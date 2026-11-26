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
from langchain_core.messages import AIMessage

from app.errors import AppError, get_trace_id
from app.graph.main_graph import build_initial_state
from app.graph.nodes import reflect
from app.models import ChatRequest
from app.services.proactive import record_emotion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# 模块级强引用：asyncio 对任务只有弱引用，局部变量无法阻止 GC。
_background_tasks: set[asyncio.Task] = set()

# 同一 thread 的图任务串行执行。
# 两轮并发写同一个 checkpoint 时，后写入的一方基于旧快照，会丢消息。
# 锁只覆盖图本身，记忆抽取在锁外后台执行，不增加用户等待。
_thread_locks: dict[str, asyncio.Lock] = {}


def _get_thread_lock(thread_id: str) -> asyncio.Lock:
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_id] = lock
    return lock


# 失败轮次补的占位回复：保证 checkpoint 里用户消息后面一定跟着 AI 消息
FAILED_REPLY = "（刚才那条没回复成功，你可以再说一次）"


async def _append_failed_reply(graph, config: dict, trace_id: str) -> None:
    """失败轮次在 checkpoint 末尾补一条 AI 占位消息。

    HumanMessage 在进入图时已经写入 checkpoint；如果 generate 失败，
    历史里会留下一条没有回复的用户消息，下一轮上下文就错位了。
    补偿本身失败也不能影响错误响应，所以整体包在 try 里。
    """
    try:
        snapshot = await graph.aget_state(config)
        messages = (snapshot.values or {}).get("messages") or []
        if not messages or getattr(messages[-1], "type", "") != "human":
            return
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(content=FAILED_REPLY)]},
        )
        logger.info("failed reply appended trace_id=%s", trace_id)
    except Exception:
        logger.exception("append failed reply error trace_id=%s", trace_id)


async def wait_background_tasks() -> None:
    """应用关闭前等待仍在跑的图任务，避免向已关闭的 checkpointer 写入。"""
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


async def _remember(final_state: dict) -> None:
    """图结束后在后台抽取记忆，不写会话 checkpoint。

    这是 S4 的时序关键：如果把抽取留在图里，慢速抽取完成时会写回
    旧快照，覆盖用户紧接着的下一轮对话。
    """
    try:
        result = await reflect(
            messages=final_state.get("messages") or [],
            branch=final_state.get("branch") or "normal",
        )
        logger.info("reflect result: %s", result)
    except Exception:
        # reflect 内部已捕获抽取失败；这里兜住意外，后台任务不能抛给事件循环
        logger.exception("reflect task crashed")


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
        completed = False
        emotion_recorded = False

        # 同一 thread 串行执行：两轮并发写同一个 checkpoint 会丢消息。
        # 锁只覆盖图，记忆抽取在 finally 里创建任务，运行在锁外。
        async with _get_thread_lock(payload.thread_id):
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

                    # 每轮只记一次情绪，供主动关怀的低情绪暂停使用
                    if not emotion_recorded and final_state.get("emotion"):
                        record_emotion(final_state["emotion"])
                        emotion_recorded = True

                    # retrieve 完成后立即告诉前端本轮命中了哪些记忆
                    if not memory_sent and "retrieve" in trace:
                        items = [
                            {"id": item["id"], "type": item["type"], "content": item["content"]}
                            for item in (final_state.get("retrieved") or [])
                        ]
                        if items:
                            await queue.put(("memory", {"items": items}))
                        memory_sent = True

                    # generate 完成后立即收尾，记忆抽取在后台继续
                    if not done_sent and "generate" in trace:
                        await _put_terminal(queue, final_state, started)
                        done_sent = True

                if not done_sent:
                    # 兜底：正常结束但没有检测到 generate 完成事件
                    await _put_terminal(queue, final_state, started)
                completed = True
            except AppError as exc:
                if not done_sent:
                    # 回复没发出去：补一条占位 AI，避免历史里留下孤立用户消息
                    await _append_failed_reply(graph, config, trace_id)
                    await queue.put(("error", {"code": exc.code, "message": exc.message}))
                else:
                    # done 已是终态，不能再补 error 事件
                    logger.warning("chat error after done: %s trace_id=%s", exc.code, trace_id)
            except Exception:
                logger.exception("chat graph failed trace_id=%s", trace_id)
                if not done_sent:
                    await _append_failed_reply(graph, config, trace_id)
                    await queue.put(("error", {"code": "INTERNAL", "message": "服务器内部错误"}))
                else:
                    logger.warning("unexpected error after done trace_id=%s", trace_id)
            finally:
                if completed and done_sent:
                    # 图已结束、done 已发：抽取放到图外后台任务，不写会话 checkpoint
                    task = asyncio.create_task(_remember(final_state))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
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
