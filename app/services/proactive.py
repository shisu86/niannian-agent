"""主动关怀（S7）。

链路：定时扫描 → 过滤（开关 / 安静时段 / 每日上限 / 低情绪）→ 取到期的 followup
→ 生成一句关怀（模型，失败用模板）→ 写 notifications → 标记 notified_at。

设计约束：
- 单用户单 worker；情绪窗口放进程内，重启后重置可接受；
- 通知与对话解耦，前端轮询展示；写入会话留到 S9；
- 任何一步失败都不能影响聊天主链路。
"""

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage

from app.config import get_settings
from app.db import connect, utc_now
from app.services.conversation import get_thread_persona
from app.services.llm import complete_json
from app.services.locks import get_thread_lock
from app.services.session import get_current_thread

logger = logging.getLogger(__name__)

# 主图实例在 lifespan 中注入，用于把主动消息写回会话历史
_graph = None


def set_graph(graph) -> None:
    global _graph
    _graph = graph


async def _append_to_session(message: str) -> None:
    """把主动消息写回当前会话，保证下一轮对话知道"自己说过话"。"""
    thread_id = get_current_thread()
    if _graph is None or not thread_id:
        return
    try:
        # 与聊天图共用同一把 thread 锁，避免并发覆盖 checkpoint
        async with get_thread_lock(thread_id):
            await _graph.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {"messages": [AIMessage(content=message, name="proactive")]},
            )
    except Exception:
        logger.exception("append proactive message to session failed")


MAX_MESSAGE_LEN = 120
MOOD_WINDOW = 3
MOOD_PAUSE_STREAK = 2
MOOD_TTL_SECONDS = 6 * 3600  # 只统计最近 6 小时的情绪，避免永久暂停
STALE_HOURS = 24  # 超过这个时间的待跟进不再补发
DEFAULT_THREAD_ID = "default"

# 最近几轮的情绪与时间；单用户，不需要按 thread 分开
_moods: deque[tuple[str, float]] = deque(maxlen=MOOD_WINDOW)

COMPOSE_PROMPT = """你是用户的陪伴者。根据下面这条待跟进的事，写一句主动关心的话。

要求：
1. 不超过 60 字，口语化，像朋友顺口提起，不要像系统提醒。
2. 不要复述原文，不要加日期数字，不要用"提醒您"。
3. 只输出 JSON：{"message":"..."}
"""


def record_emotion(emotion: str) -> None:
    """记录一轮对话的情绪与时间，供低情绪暂停使用。"""
    if isinstance(emotion, str) and emotion:
        _moods.append((emotion, time.monotonic()))


def mood_paused() -> bool:
    """最近两轮都是 low/anxious 时暂停；超过 6 小时的情绪不再计入。"""
    cutoff = time.monotonic() - MOOD_TTL_SECONDS
    recent = [emotion for emotion, ts in _moods if ts >= cutoff][-MOOD_PAUSE_STREAK:]
    return len(recent) == MOOD_PAUSE_STREAK and all(item in ("low", "anxious") for item in recent)


def _parse_hm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    """解析 HH:MM；越界或格式错误时退回默认值。"""
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, ValueError):
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def in_quiet_hours(local_now: datetime, quiet_start: str, quiet_end: str) -> bool:
    """判断是否处于免打扰时段；支持跨午夜（22:00–08:00）。"""
    start = _parse_hm(quiet_start, (22, 0))
    end = _parse_hm(quiet_end, (8, 0))
    current = (local_now.hour, local_now.minute)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


async def load_settings() -> dict:
    """读取单行设置；行不存在时返回安全默认值。"""
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM settings WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {
                "proactive_enabled": 1,
                "quiet_start": "22:00",
                "quiet_end": "08:00",
                "daily_limit": 1,
                "low_mood_pause": 1,
            }
        return dict(row)
    finally:
        await conn.close()


async def _count_sent_today(local_now: datetime) -> int:
    """统计本地今天已发送的通知数。"""
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = await connect()
    try:
        row = await (
            await conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE created_at >= ?",
                (start_utc,),
            )
        ).fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()


async def _next_due(now_utc: datetime, persona_id: int) -> dict | None:
    """取当前角色最早一条到期且未提醒过的 followup。"""
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT id, content, due_at, importance
            FROM memory_item
            WHERE (persona_id = ? OR scope = 'shared')
              AND type = 'followup'
              AND status = 'active'
              AND due_at IS NOT NULL
              AND due_at <= ?
              AND notified_at IS NULL
            ORDER BY due_at ASC
            LIMIT 1
            """,
            (persona_id, stamp),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def compose_message(memory: dict) -> str:
    """生成关怀语；模型失败时退回模板，保证通知总能发出去。"""
    content = str(memory.get("content") or "").strip()
    try:
        data = await complete_json(
            [
                {"role": "system", "content": COMPOSE_PROMPT},
                {"role": "user", "content": f"待跟进的事：{content}\n请输出 JSON。"},
            ]
        )
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:MAX_MESSAGE_LEN]
    except Exception:
        logger.exception("proactive compose failed, fallback to template")

    return f"想起你说过：{content}。别太忙忘了。"[:MAX_MESSAGE_LEN]


async def _claim(memory_id: int, now: str) -> bool:
    """原子认领到期事项；并发时只有一个调用能拿到。"""
    conn = await connect()
    try:
        cursor = await conn.execute(
            """
            UPDATE memory_item
            SET notified_at = ?, updated_at = ?
            WHERE id = ? AND notified_at IS NULL AND status = 'active'
            """,
            (now, now, memory_id),
        )
        await conn.commit()
        return cursor.rowcount == 1
    finally:
        await conn.close()


async def _release(memory_id: int) -> None:
    """生成或写通知失败时释放认领，让下一轮可以重试。"""
    conn = await connect()
    try:
        await conn.execute(
            "UPDATE memory_item SET notified_at = NULL WHERE id = ?",
            (memory_id,),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _expire_stale(cutoff: str, persona_id: int) -> int:
    """把过期太久的待跟进标成 expired，避免停机恢复后集中补发旧提醒。"""
    conn = await connect()
    try:
        cursor = await conn.execute(
            """
            UPDATE memory_item
            SET status = 'expired', updated_at = ?
            WHERE (persona_id = ? OR scope = 'shared')
              AND type = 'followup'
              AND status = 'active'
              AND notified_at IS NULL
              AND due_at IS NOT NULL
              AND due_at < ?
            """,
            (utc_now(), persona_id, cutoff),
        )
        await conn.commit()
        return cursor.rowcount
    finally:
        await conn.close()


async def _deliver(memory: dict) -> dict:
    """先原子认领，再生成与写通知。

    认领后再调模型，避免两次触发各生成一条；失败会释放认领供下轮重试。
    """
    now = utc_now()
    if not await _claim(memory["id"], now):
        return {"skipped": "claimed"}

    try:
        message = await compose_message(memory)
        # 关联当前会话，前端据此在对应会话上显示未读红点
        thread_id = get_current_thread() or DEFAULT_THREAD_ID
        conn = await connect()
        try:
            await conn.execute(
                """
                INSERT INTO notifications (thread_id, memory_id, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (thread_id, memory["id"], message, now),
            )
            await conn.commit()
        finally:
            await conn.close()
    except Exception:
        await _release(memory["id"])
        raise

    # 写回会话失败不影响通知本身
    await _append_to_session(message)

    logger.info("proactive sent memory_id=%s", memory["id"])
    return {
        "sent": True,
        "memory_id": memory["id"],
        "content": message,
    }


async def run_once(now: datetime | None = None, *, force: bool = False) -> dict:
    """跑一次主动关怀扫描。

    force=True 供演示接口使用：跳过开关、时段与上限，但仍然只发一条。
    """
    settings = await load_settings()
    local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(get_settings().tz))

    if not force:
        if not settings.get("proactive_enabled"):
            return {"skipped": "disabled"}
        if settings.get("low_mood_pause") and mood_paused():
            return {"skipped": "low_mood"}
        if in_quiet_hours(local_now, settings["quiet_start"], settings["quiet_end"]):
            return {"skipped": "quiet_hours"}

    now_utc = local_now.astimezone(UTC)
    cutoff = (now_utc - timedelta(hours=STALE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    thread_id = get_current_thread()
    if not thread_id:
        # 没有打开的会话：发出去也没人能收到，等前端注册后再发
        return {"skipped": "no_session"}

    persona_id = await get_thread_persona(thread_id)
    await _expire_stale(cutoff, persona_id)
    memory = await _next_due(now_utc, persona_id)
    if memory is None:
        # 没有到期事项优先于"到上限"：重复扫描应表现为 nothing_due
        return {"skipped": "nothing_due"}

    if not force and await _count_sent_today(local_now) >= int(settings["daily_limit"]):
        return {"skipped": "daily_limit"}

    return await _deliver(memory)


async def tick() -> None:
    """调度器入口：任何异常都只记日志，不能影响主服务。"""
    try:
        result = await run_once()
        logger.info("proactive tick: %s", result)
    except asyncio.CancelledError:
        # 服务关闭时调度器会取消正在跑的 tick；事务已回滚，下轮可重试
        logger.info("proactive tick cancelled")
        raise
    except Exception:
        logger.exception("proactive tick failed")
