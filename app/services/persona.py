"""人设读写：支持多个角色。

每个角色一行 persona；会话通过 conversation 表绑定到角色。
最早的 id=1 仍作为默认角色（首次启动兜底创建）。
"""

import asyncio
import copy
import json
import logging

from app.db import connect, utc_now

logger = logging.getLogger(__name__)

# 默认中性人设：首次启动或角色不存在时使用
DEFAULT_PERSONA = {
    "name": "念念",
    "relation": "朋友",
    "traits": ["温柔", "真诚", "有耐心"],
    "catchphrases": [],
    "values": [],
    "care_style": "先听你说完，再轻轻回应",
    "system_prompt": (
        "你是念念，用户的陪伴者。用短句、口语交流，像朋友一样自然。"
        "不主动强调身份，但用户问起时必须承认自己是 AI。"
        "不要编造没发生过的事；不要给医疗、法律、投资等专业建议。"
        "遇到危机表达时停止角色扮演。"
    ),
}

# 人设写入串行化：单进程单 worker，避免并发写入互相覆盖
_persona_lock = asyncio.Lock()


def build_fallback_prompt(
    name: str,
    relation: str,
    traits: list[str],
    catchphrases: list[str],
    care_style: str,
) -> str:
    """模型没给 system_prompt 时，用结构化字段拼一个可用版本。"""
    parts = [f"你是{name}，用户的{relation}。"]
    if traits:
        parts.append("性格：" + "、".join(traits) + "。")
    if catchphrases:
        parts.append("常用口头禅：" + "、".join(catchphrases) + "。")
    if care_style:
        parts.append("关心方式：" + care_style + "。")
    parts.append("用短句、口语交流，像真实聊天；不要编造没发生过的事；用户问起时承认自己是 AI。")
    return "".join(parts)


def _load_list(value: str, default: list) -> list:
    """解析 JSON 数组字段；格式异常时退回默认值。"""
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return data if isinstance(data, list) else default


async def ensure_persona(persona_id: int = 1) -> None:
    """确保指定角色存在；不存在时按默认人设补齐。

    多角色下不能固定建 id=1，否则用户删掉默认角色后，
    任何一次记忆写入都会把"念念"重新建出来。
    """
    conn = await connect()
    try:
        now = utc_now()
        await conn.execute(
            """
            INSERT OR IGNORE INTO persona
              (id, name, relation, traits, catchphrases, values_json, care_style,
               system_prompt, version, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
            (
                persona_id,
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
        await conn.commit()
    finally:
        await conn.close()


def _clean_text(value, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value.strip()[:limit] or default


def _clean_list(value, max_items: int, max_len: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:max_len])
    return result


def _clean_persona(data: dict) -> dict:
    """统一清洗，模型抽取、手动创建、编辑共用一套规则。"""
    name = _clean_text(data.get("name"), 20, DEFAULT_PERSONA["name"])
    relation = _clean_text(data.get("relation"), 20, DEFAULT_PERSONA["relation"])
    traits = _clean_list(data.get("traits"), 10, 30)
    catchphrases = _clean_list(data.get("catchphrases"), 10, 30)
    values = _clean_list(data.get("values"), 10, 60)
    care_style = _clean_text(data.get("care_style"), 200)
    system_prompt = _clean_text(data.get("system_prompt"), 4000)
    if not system_prompt:
        system_prompt = build_fallback_prompt(name, relation, traits, catchphrases, care_style)

    return {
        "name": name,
        "relation": relation,
        "traits": traits,
        "catchphrases": catchphrases,
        "values": values,
        "care_style": care_style,
        "system_prompt": system_prompt,
    }


def _row_to_persona(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "relation": row["relation"],
        "traits": _load_list(row["traits"], []),
        "catchphrases": _load_list(row["catchphrases"], []),
        "values": _load_list(row["values_json"], []),
        "care_style": row["care_style"],
        "system_prompt": row["system_prompt"],
        "version": row["version"],
        "is_default": row["is_default"],
        "updated_at": row["updated_at"],
    }


async def get_persona(persona_id: int = 1) -> dict:
    """读取聊天用的精简人设；角色不存在时返回默认副本。"""
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT name, relation, traits, catchphrases, values_json,
                   care_style, system_prompt
            FROM persona WHERE id = ?
            """,
            (persona_id,),
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await conn.close()

    if row is None:
        return copy.deepcopy(DEFAULT_PERSONA)

    return {
        "name": row["name"],
        "relation": row["relation"],
        "traits": _load_list(row["traits"], []),
        "catchphrases": _load_list(row["catchphrases"], []),
        "values": _load_list(row["values_json"], []),
        "care_style": row["care_style"],
        "system_prompt": row["system_prompt"] or DEFAULT_PERSONA["system_prompt"],
    }


async def get_full_persona(persona_id: int = 1) -> dict | None:
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM persona WHERE id = ?", (persona_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_persona(row) if row else None
    finally:
        await conn.close()


async def list_personas() -> list[dict]:
    """按创建顺序返回所有角色。"""
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM persona ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [_row_to_persona(row) for row in rows]
    finally:
        await conn.close()


async def create_persona(data: dict) -> dict:
    """新建角色，返回完整记录。"""
    fields = _clean_persona(data)
    async with _persona_lock:
        conn = await connect()
        try:
            now = utc_now()
            cursor = await conn.execute(
                """
                INSERT INTO persona
                  (name, relation, traits, catchphrases, values_json, care_style,
                   system_prompt, version, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    fields["name"],
                    fields["relation"],
                    json.dumps(fields["traits"], ensure_ascii=False),
                    json.dumps(fields["catchphrases"], ensure_ascii=False),
                    json.dumps(fields["values"], ensure_ascii=False),
                    fields["care_style"],
                    fields["system_prompt"],
                    now,
                    now,
                ),
            )
            new_id = cursor.lastrowid
            await conn.commit()

            async with conn.execute("SELECT * FROM persona WHERE id = ?", (new_id,)) as cur:
                row = await cur.fetchone()
            return _row_to_persona(row)
        finally:
            await conn.close()


async def update_persona(persona_id: int, data: dict) -> dict | None:
    """更新指定角色，version + 1；不存在返回 None。"""
    fields = _clean_persona(data)
    async with _persona_lock:
        conn = await connect()
        try:
            cursor = await conn.execute(
                """
                UPDATE persona
                SET name = ?, relation = ?, traits = ?, catchphrases = ?,
                    values_json = ?, care_style = ?, system_prompt = ?,
                    version = version + 1, is_default = 0, updated_at = ?
                WHERE id = ?
                """,
                (
                    fields["name"],
                    fields["relation"],
                    json.dumps(fields["traits"], ensure_ascii=False),
                    json.dumps(fields["catchphrases"], ensure_ascii=False),
                    json.dumps(fields["values"], ensure_ascii=False),
                    fields["care_style"],
                    fields["system_prompt"],
                    utc_now(),
                    persona_id,
                ),
            )
            if cursor.rowcount == 0:
                await conn.rollback()
                return None
            await conn.commit()

            async with conn.execute("SELECT * FROM persona WHERE id = ?", (persona_id,)) as cur:
                row = await cur.fetchone()
            return _row_to_persona(row)
        finally:
            await conn.close()


async def save_persona(data: dict, persona_id: int = 1) -> dict:
    """兼容旧调用：更新指定角色，不存在则新建。"""
    existing = await get_full_persona(persona_id)
    if existing is not None:
        updated = await update_persona(persona_id, data)
        return updated or existing
    return await create_persona(data)


async def delete_persona(persona_id: int) -> bool:
    """删除角色；外键级联删除会话和记忆，并清理相关通知。"""
    async with _persona_lock:
        conn = await connect()
        try:
            async with conn.execute(
                "SELECT thread_id FROM conversation WHERE persona_id = ?",
                (persona_id,),
            ) as cursor:
                thread_ids = [row["thread_id"] for row in await cursor.fetchall()]

            if thread_ids:
                placeholders = ",".join("?" for _ in thread_ids)
                # 通知表没有外键级联，必须显式清理
                await conn.execute(
                    f"DELETE FROM notifications WHERE thread_id IN ({placeholders})",  # noqa: S608
                    thread_ids,
                )

            cursor = await conn.execute("DELETE FROM persona WHERE id = ?", (persona_id,))
            if cursor.rowcount == 0:
                await conn.rollback()
                return False
            await conn.commit()
            return True
        finally:
            await conn.close()
