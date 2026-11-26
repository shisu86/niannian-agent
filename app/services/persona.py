"""人设读取。

S2 只读取已有 persona；没有时返回默认中性人设，不写库。
完整的人设塑造、interrupt 确认与版本管理在 S5 实现。
"""

import copy
import json
import logging

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
