"""会话历史与当前线程注册。

历史消息直接读 checkpointer 里的 state，不再单独存一份业务表；
前端刷新后用它恢复聊天记录。
"""

import logging

from fastapi import APIRouter, Query, Request

from app.db import connect
from app.errors import AppError, get_trace_id
from app.models import ApiResponse, SessionRegisterRequest
from app.services.conversation import (
    delete_conversation,
    get_conversation,
    list_conversations,
    upsert_conversation,
)
from app.services.session import (
    clear_current_thread,
    get_current_thread,
    set_current_thread,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/session", tags=["session"])


@router.post("/register", response_model=ApiResponse[dict])
async def register_thread(
    payload: SessionRegisterRequest,
    request: Request,
) -> ApiResponse[dict]:
    """记录前端当前使用的 thread 与角色，供聊天和主动关怀使用。"""
    await upsert_conversation(payload.thread_id, payload.persona_id)
    set_current_thread(payload.thread_id)
    return ApiResponse(
        data={"thread_id": payload.thread_id, "persona_id": payload.persona_id},
        trace_id=get_trace_id(request),
    )


@router.get("", response_model=ApiResponse[dict])
async def list_sessions(
    request: Request,
    persona_id: int | None = Query(default=None, ge=1),
) -> ApiResponse[dict]:
    """列出会话（可按角色筛选），前端左侧分组使用。"""
    items = await list_conversations(persona_id)
    return ApiResponse(data={"items": items}, trace_id=get_trace_id(request))


@router.get("/{thread_id}/messages", response_model=ApiResponse[dict])
async def list_messages(
    thread_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponse[dict]:
    """读取某个会话最近的消息，按原顺序返回。"""
    graph = request.app.state.graph
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    raw = (snapshot.values or {}).get("messages") or []

    # 从 checkpoint 历史里还原每条消息的保存时间：
    # 每次消息数增加的 checkpoint 时间，就是新增消息的大致时间。
    message_times: dict[str, str] = {}
    try:
        checkpoints = []
        async for record in graph.aget_state_history(config):
            checkpoints.append(record)
        checkpoints.reverse()

        seen = 0
        for record in checkpoints:
            record_messages = (record.values or {}).get("messages") or []
            if len(record_messages) > seen:
                stamp = getattr(record, "created_at", "") or ""
                for message in record_messages[seen:]:
                    message_times[getattr(message, "id", "")] = stamp
                seen = len(record_messages)
    except Exception:
        logger.exception("load message timestamps failed")

    items = []
    for message in raw[-limit:]:
        role = {"human": "user", "ai": "assistant"}.get(getattr(message, "type", ""))
        content = getattr(message, "content", "")
        if role and isinstance(content, str):
            message_id = getattr(message, "id", "")
            items.append(
                {
                    "id": message_id,
                    "role": role,
                    "content": content,
                    # 主动关怀消息带 name=proactive，前端据此加虚线样式
                    "name": getattr(message, "name", "") or "",
                    "created_at": message_times.get(message_id, ""),
                }
            )

    conversation = await get_conversation(thread_id)
    return ApiResponse(
        data={
            "items": items,
            "total": len(items),
            "thread_id": thread_id,
            "persona_id": conversation["persona_id"] if conversation else 1,
            "title": conversation["title"] if conversation else "",
            "current_thread": get_current_thread(),
        },
        trace_id=get_trace_id(request),
    )


@router.delete("/{thread_id}", response_model=ApiResponse[dict])
async def delete_thread(thread_id: str, request: Request) -> ApiResponse[dict]:
    """删除会话及其 checkpoint；删除当前会话时清空注册。"""
    try:
        await request.app.state.checkpointer.adelete_thread(thread_id)
    except Exception as exc:
        logger.exception("delete thread failed")
        raise AppError("INTERNAL", "删除会话失败", status=500) from exc

    # 通知没有外键级联，删除会话时一起清掉
    conn = await connect()
    try:
        await conn.execute("DELETE FROM notifications WHERE thread_id = ?", (thread_id,))
        await conn.commit()
    finally:
        await conn.close()

    await delete_conversation(thread_id)
    clear_current_thread(thread_id)
    return ApiResponse(data={"deleted": thread_id}, trace_id=get_trace_id(request))
