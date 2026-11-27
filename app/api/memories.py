"""记忆管理接口：列表、编辑、删除。

编辑内容会同步重建 FTS 行；删除会同时删业务表与索引行。
"""

import logging

from fastapi import APIRouter, Query, Request

from app.errors import AppError, get_trace_id
from app.models import ApiResponse, MemoryPatch
from app.services.memory import (
    WRITABLE_TYPES,
    delete_memory,
    list_memories,
    update_memory,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memories", tags=["memories"])

ALL_STATUS = ("active", "superseded", "expired", "completed")


@router.get("", response_model=ApiResponse[dict])
async def get_memories(
    request: Request,
    persona_id: int = Query(default=1, ge=1),
    memory_type: str | None = Query(default=None, alias="type"),
    status: str = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[dict]:
    """分页查询记忆；type/status 可筛选。"""
    if memory_type and memory_type not in WRITABLE_TYPES:
        raise AppError("INVALID_ARGUMENT", "type 不合法", status=400)
    if status not in ALL_STATUS:
        raise AppError("INVALID_ARGUMENT", "status 不合法", status=400)

    data = await list_memories(
        persona_id=persona_id,
        item_type=memory_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    return ApiResponse(data=data, trace_id=get_trace_id(request))


@router.put("/{memory_id}", response_model=ApiResponse[dict])
async def patch_memory(
    memory_id: int,
    payload: MemoryPatch,
    request: Request,
    persona_id: int = Query(default=1, ge=1),
) -> ApiResponse[dict]:
    """修改内容/重要性/时间/状态；内容变化时重建索引。"""
    patch = payload.model_dump(exclude_unset=True)
    try:
        item = await update_memory(memory_id, patch, persona_id=persona_id)
    except ValueError as exc:
        raise AppError("INVALID_ARGUMENT", str(exc), status=400) from exc

    if item is None:
        raise AppError("NOT_FOUND", "记忆不存在", status=404)
    return ApiResponse(data={"item": item}, trace_id=get_trace_id(request))


@router.delete("/{memory_id}", response_model=ApiResponse[dict])
async def remove_memory(
    memory_id: int,
    request: Request,
    persona_id: int = Query(default=1, ge=1),
) -> ApiResponse[dict]:
    """硬删除记忆，同时删除 FTS 行。"""
    if not await delete_memory(memory_id, persona_id=persona_id):
        raise AppError("NOT_FOUND", "记忆不存在", status=404)
    return ApiResponse(data={"deleted": memory_id}, trace_id=get_trace_id(request))
