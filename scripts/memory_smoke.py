"""S4 记忆抽取冒烟（零成本，用假模型）。

用临时数据库验证六件事：
1. 正常抽取写入，FTS 能检索到；
2. replaces 把旧记忆标记为 superseded，检索不再返回；
3. 非法 type / importance / 空内容被丢弃；
4. followup 缺 due_at 被丢弃，合法时间统一成 UTC；
5. crisis 分支不写记忆；
6. 模型异常时 reflect 不抛出，只记 failed。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/memory_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 关键：必须在导入 app.* 之前设置环境变量，确保用临时库，不污染 data/app.db
_TMP_DIR = tempfile.mkdtemp(prefix="niannian-memory-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

import app.services.memory as memory  # noqa: E402
from app.db import connect, init_db, utc_now  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.services.persona import (  # noqa: E402
    create_persona,
    delete_persona,
    get_full_persona,
)
from app.services.search import search, tokenize  # noqa: E402

# 预置模型返回值：按顺序消费；元素是异常实例时直接抛出
_responses: list = []
failures: list[str] = []


async def fake_complete_json(messages, **_kwargs):
    """替换 memory.complete_json，避免真实调用产生费用。"""
    result = _responses.pop(0)
    if isinstance(result, Exception):
        raise result
    return result


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


async def count_memories() -> int:
    conn = await connect()
    try:
        row = await (await conn.execute("SELECT COUNT(*) FROM memory_item")).fetchone()
        return row[0]
    finally:
        await conn.close()


async def fetch_memory(memory_id: int) -> dict | None:
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM memory_item WHERE id = ?", (memory_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def seed_old_memory() -> int:
    """建库 + 插入一条"用户住在南京"，作为被取代的旧记忆。"""
    await init_db()
    conn = await connect()
    try:
        now = utc_now()
        await conn.execute(
            "INSERT OR IGNORE INTO persona (id, name, created_at, updated_at) "
            "VALUES (1, '测试', ?, ?)",
            (now, now),
        )
        content = "用户住在南京"
        cursor = await conn.execute(
            """
            INSERT INTO memory_item
              (persona_id, type, content, content_tok, importance, status,
               created_at, updated_at)
            VALUES (1, 'fact', ?, ?, 4, 'active', ?, ?)
            """,
            (content, tokenize(content), now, now),
        )
        old_id = cursor.lastrowid
        await conn.execute(
            "INSERT INTO memory_fts(rowid, content_tok) VALUES (?, ?)",
            (old_id, tokenize(content)),
        )
        await conn.commit()
        return old_id
    finally:
        await conn.close()


async def case_missing_persona() -> None:
    """角色被删后，后台记忆写入应丢弃，而不是把角色重新建出来。"""
    await init_db()
    items = [
        {
            "type": "fact",
            "content": "用户喜欢下雨天",
            "importance": 3,
            "due_at": None,
            "replaces": [],
        }
    ]
    saved = await memory.save_memories(1, items)
    check("missing persona skips write", saved["written"] == 0, str(saved))
    check("missing persona not recreated", await get_full_persona(1) is None)


async def case_hostile_types() -> None:
    """异常类型不能让整批中断，同批重复条目应被去重。"""
    _responses.append(
        {
            "items": [
                {"type": ["fact"], "content": "类型是数组", "importance": 3},
                {"type": "fact", "content": ["数组内容"], "importance": 3},
                {"type": "fact", "content": "布尔重要性", "importance": True},
                {"type": "fact", "content": "用户爱喝咖啡", "importance": 3},
                {"type": "fact", "content": "用户爱喝咖啡", "importance": 3},
            ]
        }
    )
    items = await memory.extract_memories(1, "我平时爱喝咖啡", "")
    check("hostile types dropped + dedup", len(items) == 1, f"len={len(items)}")


async def case_missing_items_key() -> None:
    """模型顶层键写错时应抛错交给 reflect 重试，而不是静默成功。"""
    _responses.append({"memories": []})
    try:
        await memory.extract_memories(1, "今天天气不错啊", "")
    except ValueError:
        check("missing items key raises", True)
    else:
        check("missing items key raises", False)


async def case_naive_due_at() -> None:
    """模型漏写时区时，按用户本地时区解释。"""
    _responses.append(
        {
            "items": [
                {
                    "type": "followup",
                    "content": "下周五体检",
                    "importance": 3,
                    "due_at": "2026-12-04T09:00:00",
                }
            ]
        }
    )
    items = await memory.extract_memories(1, "下周五要去体检", "")
    expected = "2026-12-04T01:00:00Z"  # 上海 09:00 = UTC 01:00
    check(
        "naive due_at uses local tz",
        bool(items) and items[0]["due_at"] == expected,
        str(items[0]["due_at"] if items else None),
    )


async def case_extract_and_replace(old_id: int) -> None:
    _responses.append(
        {
            "items": [
                {
                    "type": "fact",
                    "content": "用户在杭州做产品运营",
                    "importance": 5,
                    "due_at": None,
                    "replaces": [old_id],
                }
            ]
        }
    )
    items = await memory.extract_memories(1, "我搬到杭州了，在做产品运营", "恭喜你")
    check("extract valid item", len(items) == 1, f"len={len(items)}")

    saved = await memory.save_memories(1, items)
    check("save written", saved["written"] == 1, str(saved))
    check("save superseded", saved["superseded"] == 1, str(saved))

    old = await fetch_memory(old_id)
    check("old marked superseded", bool(old) and old["status"] == "superseded")
    check("old points to new", bool(old) and old["superseded_by"] is not None)

    old_hits = await search(1, ["南京"], update_hits=False)
    check("superseded not searchable", len(old_hits) == 0, f"hits={len(old_hits)}")

    new_hits = await search(1, ["杭州"], update_hits=False)
    check("new memory searchable", bool(new_hits) and "杭州" in new_hits[0]["content"])


async def case_invalid_items_dropped() -> None:
    _responses.append(
        {
            "items": [
                {"type": "unknown", "content": "类型非法", "importance": 3},
                {"type": "fact", "content": "重要性越界", "importance": 9},
                {"type": "fact", "content": "", "importance": 3},
                {"type": "preference", "content": "用户喜欢喝咖啡", "importance": 3},
            ]
        }
    )
    items = await memory.extract_memories(1, "随便聊聊，我平时爱喝咖啡", "")
    check("invalid items dropped", len(items) == 1, f"len={len(items)}")


async def case_followup_due_at() -> None:
    _responses.append(
        {
            "items": [
                {"type": "followup", "content": "下周三有面试", "importance": 5, "due_at": None},
                {
                    "type": "followup",
                    "content": "下周四体检",
                    "importance": 4,
                    "due_at": "2026-12-03T10:00:00+08:00",
                },
            ]
        }
    )
    items = await memory.extract_memories(1, "下周三面试，下周四体检", "")
    check("followup without due dropped", len(items) == 1, f"len={len(items)}")
    expected = "2026-12-03T02:00:00Z"
    check(
        "due_at normalized to utc",
        bool(items) and items[0]["due_at"] == expected,
        str(items[0]["due_at"] if items else None),
    )


async def case_crisis_skipped() -> None:
    before = await count_memories()
    result = await nodes.reflect(
        [HumanMessage(content="我不想活了")],
        "safety",
    )
    after = await count_memories()
    check("crisis skipped", result.get("skipped") == "safety", str(result))
    check("crisis writes nothing", before == after, f"{before}->{after}")


async def case_reflect_end_to_end() -> None:
    _responses.append(
        {
            "items": [
                {
                    "type": "preference",
                    "content": "用户喜欢清晨散步",
                    "importance": 3,
                    "due_at": None,
                    "replaces": [],
                }
            ]
        }
    )
    result = await nodes.reflect(
        [
            HumanMessage(content="我每天早上都喜欢去公园散步"),
            AIMessage(content="这个习惯真好"),
        ],
        "normal",
    )
    check("reflect end-to-end", result.get("written") == 1, str(result))


async def case_cross_persona_replaces() -> None:
    """一个角色写入的共享事实，另一个角色也能取代。"""
    person_a = await create_persona({"name": "甲角色", "relation": "朋友"})
    person_b = await create_persona({"name": "乙角色", "relation": "朋友"})
    try:
        await memory.save_memories(
            person_a["id"],
            [
                {
                    "type": "fact",
                    "content": "共享事实：住在南京",
                    "importance": 3,
                    "due_at": None,
                    "replaces": [],
                }
            ],
        )
        conn = await connect()
        try:
            row = await (
                await conn.execute(
                    "SELECT id FROM memory_item WHERE persona_id = ? AND content LIKE '共享事实%'",
                    (person_a["id"],),
                )
            ).fetchone()
        finally:
            await conn.close()

        saved = await memory.save_memories(
            person_b["id"],
            [
                {
                    "type": "fact",
                    "content": "共享事实：住在杭州",
                    "importance": 3,
                    "due_at": None,
                    "replaces": [row["id"]],
                }
            ],
        )
        check("cross persona superseded", saved["superseded"] == 1, str(saved))

        conn = await connect()
        try:
            old = await (
                await conn.execute(
                    "SELECT status FROM memory_item WHERE id = ?",
                    (row["id"],),
                )
            ).fetchone()
        finally:
            await conn.close()
        check("shared marked superseded", old["status"] == "superseded", old["status"])
    finally:
        await delete_persona(person_a["id"])
        await delete_persona(person_b["id"])


async def case_llm_failure_survives() -> None:
    # reflect 会重试一次，所以准备两个异常
    _responses.extend([RuntimeError("boom"), RuntimeError("boom")])
    result = await nodes.reflect(
        [
            HumanMessage(content="我最近在准备面试，有点紧张"),
            AIMessage(content="紧张很正常"),
        ],
        "normal",
    )
    check("llm failure does not raise", result.get("failed") is True, str(result))


async def main() -> None:
    memory.complete_json = fake_complete_json
    try:
        await case_missing_persona()
        old_id = await seed_old_memory()
        await case_extract_and_replace(old_id)
        await case_invalid_items_dropped()
        await case_hostile_types()
        await case_missing_items_key()
        await case_followup_due_at()
        await case_naive_due_at()
        await case_crisis_skipped()
        await case_reflect_end_to_end()
        await case_cross_persona_replaces()
        await case_llm_failure_survives()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
