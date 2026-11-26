"""S7 主动关怀冒烟（零成本，用临时库 + 假生成）。

验证：
1. 到期的 followup 会写通知并标记 notified_at；
2. 重复扫描不会重复发；
3. 开关关闭、安静时段、低情绪、每日上限都会跳过；
4. force 跳过限制；
5. 模型失败时用模板兜底。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/proactive_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP_DIR = tempfile.mkdtemp(prefix="niannian-proactive-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import app.services.proactive as proactive  # noqa: E402
from app.db import connect, init_db, utc_now  # noqa: E402
from app.services.persona import ensure_persona  # noqa: E402
from app.services.search import tokenize  # noqa: E402

LOCAL = ZoneInfo("Asia/Shanghai")
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def local_utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 26, hour, minute, tzinfo=LOCAL).astimezone(UTC)


async def _seed_followup(due_at: str) -> int:
    await ensure_persona()
    content = "下周三上午 10:00 有面试"
    conn = await connect()
    try:
        now = utc_now()
        cursor = await conn.execute(
            """
            INSERT INTO memory_item
              (persona_id, type, content, content_tok, importance, status,
               due_at, created_at, updated_at)
            VALUES (1, 'followup', ?, ?, 5, 'active', ?, ?, ?)
            """,
            (content, tokenize(content), due_at, now, now),
        )
        await conn.execute(
            "INSERT INTO memory_fts(rowid, content_tok) VALUES (?, ?)",
            (cursor.lastrowid, tokenize(content)),
        )
        await conn.commit()
        return cursor.lastrowid
    finally:
        await conn.close()


async def _set_setting(field: str, value) -> None:
    # 字段名来自本脚本常量，不接用户输入
    conn = await connect()
    try:
        await conn.execute(
            f"UPDATE settings SET {field} = ? WHERE id = 1",  # noqa: S608
            (value,),
        )
        await conn.commit()
    finally:
        await conn.close()


async def _notification_count() -> int:
    conn = await connect()
    try:
        row = await (await conn.execute("SELECT COUNT(*) FROM notifications")).fetchone()
        return row[0]
    finally:
        await conn.close()


async def _memory_notified(memory_id: int) -> bool:
    conn = await connect()
    try:
        row = await (
            await conn.execute("SELECT notified_at FROM memory_item WHERE id = ?", (memory_id,))
        ).fetchone()
        return bool(row and row[0])
    finally:
        await conn.close()


async def _reset_notified(memory_id: int) -> None:
    conn = await connect()
    try:
        await conn.execute("UPDATE memory_item SET notified_at = NULL WHERE id = ?", (memory_id,))
        await conn.commit()
    finally:
        await conn.close()


def case_quiet_boundary() -> None:
    """跨午夜时段的边界分钟。"""
    cases = [
        (21, 59, False),
        (22, 0, True),
        (23, 59, True),
        (0, 0, True),
        (7, 59, True),
        (8, 0, False),
    ]
    for hour, minute, expected in cases:
        value = proactive.in_quiet_hours(
            datetime(2026, 11, 26, hour, minute, tzinfo=LOCAL),
            "22:00",
            "08:00",
        )
        check(f"quiet {hour:02d}:{minute:02d}", value == expected)


async def case_settings_missing() -> None:
    """settings 行缺失时应返回默认值，且 PUT 能恢复。"""
    conn = await connect()
    try:
        await conn.execute("DELETE FROM settings")
        await conn.commit()
    finally:
        await conn.close()

    settings = await proactive.load_settings()
    check(
        "settings defaults",
        settings["quiet_start"] == "22:00" and settings["daily_limit"] == 1,
        str(settings),
    )
    await init_db()  # 恢复默认行


async def main() -> None:
    case_quiet_boundary()
    await init_db()
    memory_id = await _seed_followup("2026-11-26T02:00:00Z")  # 本地 10:00，已过

    async def fake_compose(memory: dict) -> str:
        # 放慢一点，制造并发窗口，验证原子认领
        await asyncio.sleep(0.05)
        return "面试准备得怎么样了？别太紧张。"

    original_compose = proactive.compose_message
    proactive.compose_message = fake_compose

    try:
        # 1. 正常发送
        result = await proactive.run_once(now=local_utc(12))
        check("sent due followup", result.get("sent") is True, str(result))
        check("notification written", await _notification_count() == 1)
        check("memory marked notified", await _memory_notified(memory_id))

        # 2. 重复扫描
        result = await proactive.run_once(now=local_utc(12))
        check("no duplicate", result.get("skipped") == "nothing_due", str(result))

        # 3. 并发触发只发一条（原子认领）
        await _reset_notified(memory_id)
        concurrent = await asyncio.gather(
            proactive.run_once(now=local_utc(12), force=True),
            proactive.run_once(now=local_utc(12), force=True),
        )
        sent_count = len([item for item in concurrent if item.get("sent")])
        check("concurrent sends once", sent_count == 1, str(concurrent))

        # 4. 开关关闭
        await _reset_notified(memory_id)
        await _set_setting("proactive_enabled", 0)
        result = await proactive.run_once(now=local_utc(12))
        check("disabled skips", result.get("skipped") == "disabled", str(result))
        await _set_setting("proactive_enabled", 1)

        # 4. 安静时段
        result = await proactive.run_once(now=local_utc(23))
        check("quiet hours skip", result.get("skipped") == "quiet_hours", str(result))

        # 5. 低情绪暂停
        proactive._moods.clear()
        proactive.record_emotion("low")
        proactive.record_emotion("anxious")
        result = await proactive.run_once(now=local_utc(12))
        check("low mood skip", result.get("skipped") == "low_mood", str(result))
        proactive._moods.clear()

        # 6. 每日上限（今天已有 1 条）
        result = await proactive.run_once(now=local_utc(12))
        check("daily limit skip", result.get("skipped") == "daily_limit", str(result))

        # 7. force 忽略限制
        result = await proactive.run_once(now=local_utc(12), force=True)
        check("force sends", result.get("sent") is True, str(result))
        check("force marks notified", await _memory_notified(memory_id))

        # 8. 模型失败时模板兜底
        async def boom(messages, **_kwargs):
            raise RuntimeError("boom")

        original_complete = proactive.complete_json
        proactive.complete_json = boom
        try:
            message = await original_compose({"content": "体检报告出来了"})
            check("template fallback", "体检报告出来了" in message, message)
        finally:
            proactive.complete_json = original_complete

        await case_settings_missing()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
