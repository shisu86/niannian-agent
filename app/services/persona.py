"""人设读取。

S2 只读取已有 persona；没有时返回默认中性人设，不写库。
完整的人设塑造、interrupt 确认与版本管理在 S5 实现。
"""

import asyncio
import copy
import json
import logging
import sqlite3

from app.db import connect, utc_now

logger = logging.getLogger(__name__)

# 默认中性人设：用户跳过配置或尚未塑造时使用
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


# 人设写入串行化：单进程单 worker，避免"先查再插"的竞态
_persona_lock = asyncio.Lock()


def build_fallback_prompt(
    name: str,
    relation: str,
    traits: list[str],
    catchphrases: list[str],
    care_style: str,
) -> str:
    """模型没给 system_prompt 时，用结构化字段拼一个可用版本。

    不能直接退回 DEFAULT_PERSONA：那样会出现 name=妈妈、prompt=念念 的错位。
    """
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
    """解析 JSON 数组字段；格式异常时退回默认值，避免整个对话失败。"""
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    return data if isinstance(data, list) else default


async def ensure_persona() -> None:
    """确保 id=1 存在；不存在时写入默认人设。

    memory_item.persona_id 有外键约束。全新部署时 init_db 不预插 persona，
    如果第一次 chat 直接走到记忆写入，会因外键失败导致记忆永远写不进去。
    这里在写入前兜底创建，is_default=1 供 S5 区分"系统兜底"与"用户配置"。
    """
    conn = await connect()
    try:
        now = utc_now()
        await conn.execute(
            """
            INSERT OR IGNORE INTO persona
              (id, name, relation, traits, catchphrases, values_json, care_style,
               system_prompt, version, is_default, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
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
        await conn.commit()
    finally:
        await conn.close()


async def get_persona() -> dict:
    """读取 id=1 的人设；不存在时返回默认人设副本。"""
    conn = await connect()
    try:
        async with conn.execute(
            """
            SELECT name, relation, traits, catchphrases, values_json,
                   care_style, system_prompt
            FROM persona WHERE id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await conn.close()

    if row is None:
        # 深拷贝，避免调用方修改列表后污染模块级默认值
        return copy.deepcopy(DEFAULT_PERSONA)

    return {
        "name": row["name"],
        "relation": row["relation"],
        "traits": _load_list(row["traits"], []),
        "catchphrases": _load_list(row["catchphrases"], []),
        "values": _load_list(row["values_json"], []),
        "care_style": row["care_style"],
        # system_prompt 为空时用默认值兜底，保证聊天可用
        "system_prompt": row["system_prompt"] or DEFAULT_PERSONA["system_prompt"],
    }


def _clean_text(value, limit: int, default: str = "") -> str:
    """字符串字段清洗：非字符串丢弃、去空白、截断。"""
    if not isinstance(value, str):
        return default
    return value.strip()[:limit] or default


def _clean_list(value, max_items: int, max_len: int) -> list[str]:
    """列表字段清洗：只保留非空字符串，并限制条数与单条长度。"""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:max_items]:
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:max_len])
    return result


def _row_to_persona(row) -> dict:
    """数据库行 → 对外人设对象（values_json 反向映射成 values）。"""
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


async def get_full_persona() -> dict | None:
    """读取完整人设（含 id/version/is_default）；不存在返回 None。"""
    conn = await connect()
    try:
        async with conn.execute("SELECT * FROM persona WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        return _row_to_persona(row) if row else None
    finally:
        await conn.close()


async def save_persona(data: dict) -> dict:
    """写入人设：存在则 version + 1，不存在则新建；返回完整记录。

    无论模型抽取还是用户直接编辑，都走这里，保证字段清洗规则一致。
    进程内用锁串行化；INSERT 再兜一层 IntegrityError，防止并发首次写入撞主键。
    """
    name = _clean_text(data.get("name"), 20, DEFAULT_PERSONA["name"])
    relation = _clean_text(data.get("relation"), 20, DEFAULT_PERSONA["relation"])
    traits = _clean_list(data.get("traits"), 10, 30)
    catchphrases = _clean_list(data.get("catchphrases"), 10, 30)
    values = _clean_list(data.get("values"), 10, 60)
    care_style = _clean_text(data.get("care_style"), 200)
    system_prompt = _clean_text(data.get("system_prompt"), 4000)
    if not system_prompt:
        # 按本次字段拼兜底，不能退回"念念"的默认 prompt
        system_prompt = build_fallback_prompt(name, relation, traits, catchphrases, care_style)

    insert_sql = """
        INSERT INTO persona
          (id, name, relation, traits, catchphrases, values_json, care_style,
           system_prompt, version, is_default, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
        """
    update_sql = """
        UPDATE persona
        SET name = ?, relation = ?, traits = ?, catchphrases = ?,
            values_json = ?, care_style = ?, system_prompt = ?,
            version = version + 1, is_default = 0, updated_at = ?
        WHERE id = 1
        """
    params = (
        name,
        relation,
        json.dumps(traits, ensure_ascii=False),
        json.dumps(catchphrases, ensure_ascii=False),
        json.dumps(values, ensure_ascii=False),
        care_style,
        system_prompt,
    )

    async with _persona_lock:
        conn = await connect()
        try:
            now = utc_now()
            async with conn.execute("SELECT version FROM persona WHERE id = 1") as cursor:
                exists = await cursor.fetchone()

            if exists is None:
                try:
                    await conn.execute(insert_sql, (*params, now, now))
                except sqlite3.IntegrityError:
                    # 极端并发下另一个写入刚插入，退回更新
                    await conn.execute(update_sql, (*params, now))
            else:
                await conn.execute(update_sql, (*params, now))
            await conn.commit()

            async with conn.execute("SELECT * FROM persona WHERE id = 1") as cursor:
                saved = await cursor.fetchone()
            return _row_to_persona(saved)
        finally:
            await conn.close()
