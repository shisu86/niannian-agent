"""会话与角色的绑定。

一个 thread 属于一个角色：聊天时按它取 system_prompt 和检索范围。
"""

import logging

from app.db import connect, utc_now

logger = logging.getLogger(__name__)


async def upsert_conversation(thread_id: str, persona_id: int, title: str = "") -> None:
    """新建或更新会话绑定；空标题不覆盖已有标题。"""
    conn = await connect()
    try:
        now = utc_now()
        await conn.execute(
            """
            INSERT INTO conversation (thread_id, persona_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            -- 已存在的 thread 不因前端注册而改绑角色，避免历史串到别人名下
            ON CONFLICT(thread_id) DO UPDATE SET
              title = CASE
                WHEN excluded.title != '' THEN excluded.title
                ELSE conversation.title
              END,
              updated_at = excluded.updated_at
            """,
            (thread_id, persona_id, title, now, now),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_conversation(thread_id: str) -> dict | None:
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT thread_id, persona_id, title, created_at, updated_at
            FROM conversation WHERE thread_id = ?
            """,
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def list_conversations(persona_id: int | None = None) -> list[dict]:
    conn = await connect()
    try:
        if persona_id is None:
            sql = """
                SELECT thread_id, persona_id, title, created_at
                FROM conversation ORDER BY updated_at DESC
                """
            params: tuple = ()
        else:
            sql = """
                SELECT thread_id, persona_id, title, created_at
                FROM conversation WHERE persona_id = ? ORDER BY updated_at DESC
                """
            params = (persona_id,)

        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def delete_conversation(thread_id: str) -> bool:
    conn = await connect()
    try:
        cursor = await conn.execute("DELETE FROM conversation WHERE thread_id = ?", (thread_id,))
        if cursor.rowcount == 0:
            await conn.rollback()
            return False
        await conn.commit()
        return True
    finally:
        await conn.close()


async def get_thread_persona(thread_id: str) -> int:
    """聊天时用：返回线程绑定的角色；没有记录时退回默认角色 1。"""
    conversation = await get_conversation(thread_id)
    return int(conversation["persona_id"]) if conversation else 1
