"""POST /api/chat：SSE 流式对话。

事件顺序：meta → token* → trace → done；失败时以 error 收尾。
trace/done 在 generate 完成时立即发送，reflect 在后台继续，
保证记忆沉淀不阻塞用户感知。

图在独立 asyncio 任务中执行，SSE 只消费队列。
任务由模块级集合持有强引用，避免客户端断开后被垃圾回收。
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.errors import AppError, get_trace_id
from app.graph.main_graph import build_initial_state
from app.models import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# 模块级强引用：asyncio 对任务只有弱引用，局部变量无法阻止 GC。
_background_tasks: set[asyncio.Task] = set()


async def wait_background_tasks() -> None:
    """应用关闭前等待仍在跑的图任务，避免向已关闭的 checkpointer 写入。"""
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


def _sse(event: str, data: dict) -> str:
    """SSE 事件块：event 行 + 单行 JSON + 空行。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _put_terminal(
    queue: asyncio.Queue,
    final_state: dict,
    started: float,
) -> None:
    """发送本轮终态：trace + done。"""
    await queue.put(
        (
            "trace",
            {
                "branch": final_state.get("branch"),
                "emotion": final_state.get("emotion"),
                "queries": final_state.get("queries", []),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            },
        )
    )
    messages = final_state.get("messages") or []
    await queue.put(
        (
            "done",
            {
                "message_id": getattr(messages[-1], "id", "") if messages else "",
                "usage": {},
            },
        )
    )


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    """接收一条用户消息，返回流式回复。"""
    graph = request.app.state.graph
    trace_id = get_trace_id(request)
    config = {"configurable": {"thread_id": payload.thread_id}}
    initial = await build_initial_state(payload.message)

    # 无界队列：单轮数据量小，不需要背压
    queue: asyncio.Queue = asyncio.Queue()

    async def run_graph() -> None:
        final_state: dict = {}
        started = time.perf_counter()
        done_sent = False
        memory_sent = False
        try:
            async for mode, chunk in graph.astream(
                initial,
                config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom":
                    # generate 节点通过 get_stream_writer 写出 token
                    if isinstance(chunk, dict) and chunk.get("type") == "token":
                        await queue.put(("token", {"text": chunk.get("text", "")}))
                    continue

                final_state = chunk
                trace = final_state.get("trace") or {}

                # retrieve 完成后立即告诉前端本轮命中了哪些记忆
                if not memory_sent and "retrieve" in trace:
                    items = [
                        {"id": item["id"], "type": item["type"], "content": item["content"]}
                        for item in (final_state.get("retrieved") or [])
                    ]
                    if items:
                        await queue.put(("memory", {"items": items}))
                    memory_sent = True

                # generate 完成后立即收尾，后面的 reflect 仍会在后台跑完
                if not done_sent and "generate" in trace:
                    await _put_terminal(queue, final_state, started)
                    done_sent = True

            if not done_sent:
                # 兜底：正常结束但没有检测到 generate 完成事件
                await _put_terminal(queue, final_state, started)
        except AppError as exc:
            await queue.put(("error", {"code": exc.code, "message": exc.message}))
        except Exception:
            logger.exception("chat graph failed trace_id=%s", trace_id)
            await queue.put(("error", {"code": "INTERNAL", "message": "服务器内部错误"}))
        finally:
            await queue.put(("__end__", None))

    async def event_stream():
        # meta 必须是第一个事件
        yield _sse("meta", {"trace_id": trace_id, "thread_id": payload.thread_id})
        task = asyncio.create_task(run_graph())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        try:
            while True:
                kind, data = await queue.get()
                if kind == "__end__":
                    break
                yield _sse(kind, data)
        finally:
            # 客户端断开时不要取消图任务：reflect 仍需跑完
            _ = task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
