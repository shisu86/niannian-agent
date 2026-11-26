"""主动关怀与设置接口。

- GET  /api/followups      待跟进列表
- GET  /api/notifications  取未读通知（读取即标记已读）
- POST /api/proactive/trigger  手动触发一次（演示用，跳过时段与上限）
- GET/PUT /api/settings    关怀与语音设置
"""

import logging

from fastapi import APIRouter, Request

from app.db import connect, utc_now
from app.errors import get_trace_id
from app.models import ApiResponse, SettingsPayload
from app.services.proactive import run_once

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["proactive"])

SETTINGS_FIELDS = (
    "proactive_enabled",
    "quiet_start",
    "quiet_end",
    "daily_limit",
    "low_mood_pause",
    "voice_name",
    "auto_read",
    "asr_enabled",
)

DEFAULT_SETTINGS = {
    "proactive_enabled": 1,
    "quiet_start": "22:00",
    "quiet_end": "08:00",
    "daily_limit": 1,
    "low_mood_pause": 1,
    "voice_name": "zh-CN-XiaoxiaoNeural",
    "auto_read": 0,
    "asr_enabled": 1,
}


@router.get("/followups", response_model=ApiResponse[dict])
async def list_followups(request: Request) -> ApiResponse[dict]:
    """返回所有 active 的待跟进事项，按时间升序。"""
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT id, content, due_at, importance, notified_at
            FROM memory_item
            WHERE persona_id = 1 AND type = 'followup' AND status = 'active'
            ORDER BY due_at ASC, id ASC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        items = [dict(row) for row in rows]
    finally:
        await conn.close()

    return ApiResponse(data={"items": items, "total": len(items)}, trace_id=get_trace_id(request))


@router.get("/notifications", response_model=ApiResponse[dict])
async def take_notifications(request: Request) -> ApiResponse[dict]:
    """取未读通知并标记已读；前端轮询这个接口即可。"""
    conn = await connect()
    try:
        # UPDATE ... RETURNING 一步完成"读取未读 + 标记已读"，
        # 两个并发请求只有一方能拿到行，不会重复展示
        async with conn.execute(
            """
            UPDATE notifications
            SET read_at = ?
            WHERE read_at IS NULL
            RETURNING id, thread_id, memory_id, content, created_at
            """,
            (utc_now(),),
        ) as cursor:
            rows = await cursor.fetchall()
        await conn.commit()
        items = [dict(row) for row in rows]
    finally:
        await conn.close()

    return ApiResponse(data={"items": items}, trace_id=get_trace_id(request))


@router.post("/proactive/trigger", response_model=ApiResponse[dict])
async def trigger_proactive(request: Request) -> ApiResponse[dict]:
    """手动触发一次主动关怀，便于演示；跳过时段、开关与每日上限。"""
    result = await run_once(force=True)
    return ApiResponse(data=result, trace_id=get_trace_id(request))


@router.get("/settings", response_model=ApiResponse[dict])
async def get_settings_api(request: Request) -> ApiResponse[dict]:
    """读取关怀与语音设置。"""
    conn = await connect()
    try:
        # 列名写死，不用 f-string 拼 SQL
        async with conn.execute(
            """
            SELECT proactive_enabled, quiet_start, quiet_end, daily_limit,
                   low_mood_pause, voice_name, auto_read, asr_enabled
            FROM settings WHERE id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await conn.close()

    # 行缺失时返回默认值，而不是一组 None
    data = dict(row) if row else dict(DEFAULT_SETTINGS)
    return ApiResponse(data={"settings": data}, trace_id=get_trace_id(request))


@router.put("/settings", response_model=ApiResponse[dict])
async def update_settings(payload: SettingsPayload, request: Request) -> ApiResponse[dict]:
    """全量更新设置；settings 行由 init_db 保证存在。"""
    values = payload.model_dump()
    now = utc_now()

    conn = await connect()
    try:
        # UPSERT：settings 行被删掉时也能恢复，而不是返回 200 却什么都没写
        await conn.execute(
            """
            INSERT INTO settings
              (id, proactive_enabled, quiet_start, quiet_end, daily_limit,
               low_mood_pause, voice_name, auto_read, asr_enabled, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              proactive_enabled = excluded.proactive_enabled,
              quiet_start = excluded.quiet_start,
              quiet_end = excluded.quiet_end,
              daily_limit = excluded.daily_limit,
              low_mood_pause = excluded.low_mood_pause,
              voice_name = excluded.voice_name,
              auto_read = excluded.auto_read,
              asr_enabled = excluded.asr_enabled,
              updated_at = excluded.updated_at
            """,
            (*[values[field] for field in SETTINGS_FIELDS], now),
        )
        await conn.commit()
    finally:
        await conn.close()

    return ApiResponse(
        data={"settings": values},
        trace_id=get_trace_id(request),
    )
