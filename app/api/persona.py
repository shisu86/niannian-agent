"""人设接口：提取、确认、默认、读取与编辑。

确认流程依赖 LangGraph 的 interrupt：
extract 接口把子图跑到 interrupt 处暂停，checkpointer 保存现场；
confirm 接口用 Command(resume=...) 从暂停处继续，save 节点写库。
是否"真的还挂着"以 checkpointer 快照为准，进程内字典只负责超时判断。
"""

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Request
from langgraph.types import Command

from app.errors import AppError, get_trace_id
from app.models import (
    ApiResponse,
    PersonaConfirmRequest,
    PersonaExtractRequest,
    PersonaPayload,
)
from app.services.persona import ensure_persona, get_full_persona, save_persona

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/persona", tags=["persona"])

# 人设确认有效期：超过后需要重新提取
PENDING_TTL_SECONDS = 30 * 60

# 只记录创建时间；单 worker 下重启后作废是可接受的
_pending: dict[str, float] = {}

# 同一 thread 的确认串行化：两个请求同时 resume 会各自写一次库
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(thread_id: str) -> asyncio.Lock:
    lock = _locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[thread_id] = lock
    return lock


async def _purge_expired(request: Request) -> None:
    """清理过期确认：删内存记录，并删掉对应 checkpoint。

    每次 extract 都会新建 thread；不确认的 thread 会一直占着
    checkpoints.db（最多 500 条对话），过期时一并删除。
    """
    now = time.monotonic()
    expired = [key for key, created in _pending.items() if now - created > PENDING_TTL_SECONDS]
    if not expired:
        return

    checkpointer = request.app.state.checkpointer
    for key in expired:
        _pending.pop(key, None)
        try:
            await checkpointer.adelete_thread(key)
        except Exception:
            logger.warning("delete expired persona thread failed: %s", key)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _persona_graph(request: Request):
    """从应用状态取编译好的子图；不要每个请求重新编译。"""
    return request.app.state.persona_graph


@router.post("/extract", response_model=ApiResponse[dict])
async def extract(
    payload: PersonaExtractRequest,
    request: Request,
) -> ApiResponse[dict]:
    """提取候选人格，运行到 interrupt 后返回。"""
    graph = _persona_graph(request)
    await _purge_expired(request)
    thread_id = f"p-{uuid.uuid4().hex[:8]}"
    config = _config(thread_id)

    initial = {
        "messages": [item.model_dump() for item in payload.messages],
        "name_hint": payload.name_hint,
        "relation_hint": payload.relation_hint,
    }
    # interrupt 时 ainvoke 正常返回，现场保存在 checkpointer 里
    await graph.ainvoke(initial, config)

    snapshot = await graph.aget_state(config)
    candidates = (snapshot.values or {}).get("candidates")
    if not candidates:
        raise AppError("LLM_ERROR", "人设提取失败，请重试", status=502)

    _pending[thread_id] = time.monotonic()
    return ApiResponse(
        data={"persona_thread_id": thread_id, "candidates": candidates},
        trace_id=get_trace_id(request),
    )


@router.get("/pending", response_model=ApiResponse[dict])
async def pending(persona_thread_id: str, request: Request) -> ApiResponse[dict]:
    """按 persona_thread_id 取回未确认的候选，供页面刷新后恢复。"""
    graph = _persona_graph(request)
    await _purge_expired(request)
    if persona_thread_id not in _pending:
        raise AppError("INTERRUPT_NOT_PENDING", "没有待确认的人设", status=409)

    snapshot = await graph.aget_state(_config(persona_thread_id))
    candidates = (snapshot.values or {}).get("candidates")
    # 只有停在 confirm 节点才算"待确认"；停在 save 说明上次写库失败
    if not candidates or tuple(snapshot.next or ()) != ("confirm",):
        _pending.pop(persona_thread_id, None)
        raise AppError("INTERRUPT_NOT_PENDING", "没有待确认的人设", status=409)

    return ApiResponse(
        data={"candidates": candidates},
        trace_id=get_trace_id(request),
    )


@router.post("/confirm", response_model=ApiResponse[dict])
async def confirm(
    payload: PersonaConfirmRequest,
    request: Request,
) -> ApiResponse[dict]:
    """恢复子图，把确认后的人设写进 persona 表。"""
    graph = _persona_graph(request)
    await _purge_expired(request)
    thread_id = payload.persona_thread_id
    config = _config(thread_id)

    # 同一 thread 串行：两个标签页同时确认会各自 resume 一次
    async with _get_lock(thread_id):
        if thread_id not in _pending:
            raise AppError("INTERRUPT_NOT_PENDING", "人设确认已失效，请重新提取", status=409)

        snapshot = await graph.aget_state(config)
        # 必须真的停在 confirm 节点；停在 save 说明上次写库失败，重跑会用旧 state
        if tuple(snapshot.next or ()) != ("confirm",):
            _pending.pop(thread_id, None)
            raise AppError("INTERRUPT_NOT_PENDING", "人设确认已失效，请重新提取", status=409)

        try:
            await graph.ainvoke(Command(resume=payload.persona.model_dump()), config)
        except Exception as exc:
            # 写库失败：清掉 pending，要求重新提取，避免用旧 state 重试
            _pending.pop(thread_id, None)
            logger.exception("persona confirm failed")
            raise AppError("INTERNAL", "人设保存失败，请重新提取", status=500) from exc

        snapshot = await graph.aget_state(config)
        persona = (snapshot.values or {}).get("persona")
        if tuple(snapshot.next or ()) or not persona:
            _pending.pop(thread_id, None)
            raise AppError("INTERNAL", "人设保存失败，请重新提取", status=500)

        _pending.pop(thread_id, None)
        logger.info("persona confirmed version=%s", persona.get("version"))
        return ApiResponse(
            data={"persona": persona, "version": persona.get("version")},
            trace_id=get_trace_id(request),
        )


@router.post("/default", response_model=ApiResponse[dict])
async def create_default(request: Request) -> ApiResponse[dict]:
    """跳过配置：已有人设直接返回，没有则创建默认人设（幂等）。"""
    persona = await get_full_persona()
    if persona is None:
        await ensure_persona()
        persona = await get_full_persona()

    return ApiResponse(
        data={"persona": persona},
        trace_id=get_trace_id(request),
    )


@router.get("", response_model=ApiResponse[dict])
async def get_persona_api(request: Request) -> ApiResponse[dict]:
    """读取完整人设。"""
    persona = await get_full_persona()
    if persona is None:
        raise AppError("PERSONA_NOT_FOUND", "还没有配置人设", status=404)

    return ApiResponse(data={"persona": persona}, trace_id=get_trace_id(request))


@router.put("", response_model=ApiResponse[dict])
async def update_persona(payload: PersonaPayload, request: Request) -> ApiResponse[dict]:
    """直接编辑人设（确认弹窗之外的手动修改也走这里）。"""
    persona = await save_persona(payload.model_dump())
    return ApiResponse(
        data={"persona": persona, "version": persona.get("version")},
        trace_id=get_trace_id(request),
    )
