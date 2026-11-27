"""S9 记忆管理与会话存储冒烟（零成本）。

验证：
1. 列表分页与总数；
2. 修改内容后 FTS 行被重建（旧内容搜不到、新内容搜得到）；
3. 删除记忆时业务表与 FTS 行一起删除；
4. 当前会话线程的注册与读取。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/session_memory_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="niannian-session-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from app.db import connect, init_db  # noqa: E402
from app.services.memory import (  # noqa: E402
    delete_memory,
    list_memories,
    save_memories,
    update_memory,
)
from app.services.persona import ensure_persona  # noqa: E402
from app.services.search import search  # noqa: E402
from app.services.session import get_current_thread, set_current_thread  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


async def count_fts(memory_id: int) -> int:
    conn = await connect()
    try:
        row = await (
            await conn.execute(
                "SELECT COUNT(*) FROM memory_fts WHERE rowid = ?",
                (memory_id,),
            )
        ).fetchone()
        return row[0]
    finally:
        await conn.close()


async def main() -> None:
    await init_db()
    await ensure_persona()

    await save_memories(
        1,
        [
            {
                "type": "fact",
                "content": "用户住在南京",
                "importance": 3,
                "due_at": None,
                "replaces": [],
            }
        ],
    )

    data = await list_memories(1)
    check("list total", data["total"] == 1, str(data["total"]))
    memory_id = data["items"][0]["id"]

    hits = await search(1, ["南京"], update_hits=False)
    check("search before update", len(hits) == 1, str(len(hits)))

    updated = await update_memory(memory_id, {"content": "用户住在杭州", "importance": 5})
    check("update content", updated is not None and updated["content"] == "用户住在杭州")
    check("update importance", updated is not None and updated["importance"] == 5)

    old_hits = await search(1, ["南京"], update_hits=False)
    check("old content gone", len(old_hits) == 0, str(len(old_hits)))
    new_hits = await search(1, ["杭州"], update_hits=False)
    check("new content searchable", len(new_hits) == 1, str(len(new_hits)))

    deleted = await delete_memory(memory_id)
    check("delete returns true", deleted is True)
    check("fts row removed", await count_fts(memory_id) == 0)
    remaining = await list_memories(1)
    check("list empty after delete", remaining["total"] == 0, str(remaining["total"]))
    check("delete missing returns false", await delete_memory(999999) is False)

    set_current_thread("thread-smoke-1234")
    check("session registered", get_current_thread() == "thread-smoke-1234")

    shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
