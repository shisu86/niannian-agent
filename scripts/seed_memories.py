"""写入 S3 演示用的长期记忆。

用法（项目根目录执行）：
    ./.venv/Scripts/python.exe scripts/seed_memories.py
    ./.venv/Scripts/python.exe scripts/seed_memories.py --reset   # 先清空再写

行为：
- 确保 persona(id=1) 存在，不存在则创建默认人设；
- 已有记忆时默认跳过，避免重复插入；--reset 会清空记忆、通知与 FTS。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from app.db import connect, init_db, utc_now  # noqa: E402
from app.services.persona import DEFAULT_PERSONA  # noqa: E402
from app.services.search import tokenize  # noqa: E402

# (type, content, importance, due_at)
MEMORIES = [
    ("fact", "用户在一家互联网公司做运营", 4, None),
    ("fact", "用户因为工作换到了杭州", 5, None),
    ("fact", "用户有胃病，不能吃太辣", 4, None),
    ("fact", "用户养了一只叫团子的猫", 3, None),
    ("preference", "用户不吃香菜", 4, None),
    ("preference", "用户不喜欢被催，越催越烦", 4, None),
    ("event", "用户上周和朋友去看了演唱会，很开心", 3, None),
    ("event", "用户上个月搬了家，离公司更近了", 3, None),
    ("relationship", "用户和室友最近有点矛盾，不太想回宿舍", 4, None),
    ("relationship", "用户和妈妈每周通一次电话", 3, None),
    ("followup", "下周三上午 10:00 有面试", 5, "2026-12-02T02:00:00Z"),
    ("followup", "体检报告出来了，记得看", 4, "2026-11-28T02:00:00Z"),
]

PERSONA_SQL = """
    INSERT OR IGNORE INTO persona
      (id, name, relation, traits, catchphrases, values_json, care_style,
       system_prompt, version, is_default, created_at, updated_at)
    VALUES (1, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
"""


async def seed(reset: bool) -> None:
    # 新库直接跑脚本时也先建表，避免 "no such table"
    await init_db()
    conn = await connect()
    try:
        now = utc_now()
        await conn.execute(
            PERSONA_SQL,
            (
                DEFAULT_PERSONA["name"],
                DEFAULT_PERSONA["relation"],
                json.dumps(DEFAULT_PERSONA["traits"], ensure_ascii=False),
                json.dumps(DEFAULT_PERSONA["catchphrases"], ensure_ascii=False),
                json.dumps(DEFAULT_PERSONA["values"], ensure_ascii=False),
                DEFAULT_PERSONA["care_style"],
                DEFAULT_PERSONA["system_prompt"],
                now,
                now,
            ),
        )

        count = (await (await conn.execute("SELECT COUNT(*) FROM memory_item")).fetchone())[0]
        if count and not reset:
            print(f"已有 {count} 条记忆，跳过。需要重置请加 --reset")
            await conn.commit()
            return

        if reset:
            await conn.execute("DELETE FROM notifications")
            await conn.execute("DELETE FROM memory_fts")
            await conn.execute("DELETE FROM memory_item")

        for item_type, content, importance, due_at in MEMORIES:
            async with conn.execute(
                """
                INSERT INTO memory_item
                  (persona_id, type, content, content_tok, importance, scope, status,
                   due_at, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    item_type,
                    content,
                    tokenize(content),
                    importance,
                    "shared" if item_type == "fact" else "personal",
                    due_at,
                    now,
                    now,
                ),
            ) as cursor:
                memory_id = cursor.lastrowid
            await conn.execute(
                "INSERT INTO memory_fts(rowid, content_tok) VALUES (?, ?)",
                (memory_id, tokenize(content)),
            )

        await conn.commit()
        print(f"写入 {len(MEMORIES)} 条记忆，persona(id=1) 已就绪")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed("--reset" in sys.argv))
