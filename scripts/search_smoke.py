"""S3 检索冒烟。

前置：先运行 scripts/seed_memories.py。
用法：
    ./.venv/Scripts/python.exe scripts/search_smoke.py

验证中文关键词能命中对应记忆；任何一项失败退出码为 1。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from app.db import connect  # noqa: E402
from app.services.search import search  # noqa: E402

CASES = [
    ("下周三面试", "面试"),
    ("胃病", "胃病"),
    ("香菜", "香菜"),
    ("室友", "室友"),
    # 自然问句：验证 OR 召回后，相关记忆仍能排到第一
    ("我最近胃病犯了不能吃辣", "胃病"),
    ("我下周三要做什么？", "面试"),
]


async def main() -> None:
    conn = await connect()
    try:
        count = (await (await conn.execute("SELECT COUNT(*) FROM memory_item")).fetchone())[0]
    finally:
        await conn.close()

    if count == 0:
        raise SystemExit("记忆表为空，请先运行 scripts/seed_memories.py")

    failures: list[str] = []
    for query, expected in CASES:
        hits = await search(persona_id=1, queries=[query], update_hits=False)
        first = hits[0]["content"] if hits else ""
        ok = expected in first
        print(f"{'OK  ' if ok else 'FAIL'} {query:18s} -> {len(hits)} 条，Top1：{first[:30]}")
        for item in hits:
            print(f"      [{item['type']}] {item['content']} (score={item['score']})")
        if not ok:
            failures.append(query)

    # 控制字符不应让 FTS5 抛 "unterminated string"
    try:
        control_hits = await search(persona_id=1, queries=["\x00"], update_hits=False)
        print(f"OK   control char -> {len(control_hits)} 条")
    except Exception as exc:
        print(f"FAIL control char -> {type(exc).__name__}")
        failures.append("control")

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
