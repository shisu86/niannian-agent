"""LLM 意图与情绪分析（S6）。

一次模型调用同时产出：intent、emotion、risk_level、queries、followup_hint。
调用方（graph/nodes.py）负责：
- 危机规则先跑，命中就不调这里；
- 这里抛出的任何异常都降级到规则版，不阻断对话。
"""

import asyncio
import logging
import re

from app.services.llm import complete_json

logger = logging.getLogger(__name__)

# 首 token 延迟直接受这里影响：宁可降级规则版，也不让用户等太久
ANALYZE_TIMEOUT_SECONDS = 5.0

MAX_QUERIES = 3
MAX_QUERY_LEN = 50
MAX_HISTORY_CHARS = 2000
MAX_USER_TEXT_CHARS = 1000
MAX_FOLLOWUPS = 3

# 零宽字符会把 query 变成肉眼看不见的"空词"，检索时命中 0 条
_IGNORED_CHARS = re.compile(r"[\u200b-\u200f\u2028-\u202f\ufeff]+")

INTENTS = {"chat", "vent", "help", "recall", "farewell"}
EMOTIONS = {"calm", "low", "anxious", "happy"}
RISK_LEVELS = {"normal", "elevated", "crisis"}

ANALYZE_PROMPT = """你是对话分析器。根据最近对话和用户最新一条消息，判断意图、情绪、风险，
并生成用于检索长期记忆的查询。

只输出 JSON：
{"intent":"chat|vent|help|recall|farewell",
 "emotion":"calm|low|anxious|happy",
 "risk_level":"normal|elevated|crisis",
 "queries":["查询1","查询2"],
 "followup_hint":[{"content":"待跟进的事","due_at":null}]}

规则：
1. intent：chat=闲聊，vent=倾诉，help=求助或要建议，recall=回忆过去，farewell=告别。
2. emotion：calm=平稳，low=低落/疲惫，anxious=焦虑/紧张，happy=开心。
3. risk_level 只看用户最新一条消息：该消息表达自伤、自杀、绝望时给 crisis；
   明显的强烈痛苦但不涉及自伤时给 elevated；
   单纯的问候、寒暄（你好、在吗、早上好）为 normal；
   短句本身含自伤、自杀含义时仍必须给 crisis；
   历史消息中的危机表达不代表本轮风险，只用于理解语境。
4. queries 用于检索与用户最新消息相关的长期记忆，2-3 条，每条不超过 20 字；
   一条取原意关键词，一条可做同义改写；不要检索历史里提过、但本轮没提的话题。
5. followup_hint 是以后要跟进的具体事项，没有就返回空数组；due_at 可留 null。
6. 只根据对话判断，不要编造。

只输出 JSON，不要任何解释。"""


def clean_queries(value, fallback: str) -> list[str]:
    """清洗检索 query：去零宽/空白、去重、限长、限条数；全空时用原句兜底。

    规则版降级路径也复用这里，避免把整段 2000 字原文直接塞进 FTS。
    """
    queries: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                continue
            text = _IGNORED_CHARS.sub("", item).strip()[:MAX_QUERY_LEN]
            if text and text not in queries:
                queries.append(text)
            if len(queries) >= MAX_QUERIES:
                break

    if queries:
        return queries
    cleaned_fallback = _IGNORED_CHARS.sub("", fallback).strip()[:MAX_QUERY_LEN]
    return [cleaned_fallback] if cleaned_fallback else []


def _enum(value, allowed: set[str]) -> str | None:
    """枚举字段归一化：模型偶尔返回大写或带空格，先规整再校验。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in allowed else None


def _clean_followups(value) -> list[dict]:
    """清洗待跟进线索；S6 只产出、不落库，S7 再使用。"""
    followups: list[dict] = []
    if not isinstance(value, list):
        return followups
    for item in value[:MAX_FOLLOWUPS]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        due_at = item.get("due_at")
        followups.append(
            {
                "content": content.strip()[:100],
                "due_at": due_at if isinstance(due_at, str) else None,
            }
        )
    return followups


def _build_input(user_text: str, history: list[dict]) -> str:
    """拼最近对话 + 最新消息；按字符预算保留靠后的内容。"""
    lines: list[str] = []
    budget = MAX_HISTORY_CHARS
    for item in reversed(history):
        role = "用户" if item.get("role") == "user" else "助理"
        content = str(item.get("content") or "")
        prefix = f"{role}："
        budget -= len(prefix)
        if budget <= 0:
            break
        if len(content) > budget:
            content = content[-budget:]
        lines.append(prefix + content)
        budget -= len(content)
        if budget <= 0:
            break
    lines.reverse()

    context = "\n".join(lines) or "（无历史）"
    # 最新消息也限长，避免接近 2000 字的长文把输入撑大、拖到超时
    latest = user_text[:MAX_USER_TEXT_CHARS]
    return f"最近对话：\n{context}\n\n用户最新消息：{latest}\n\n请输出 JSON。"


SECOND_CHECK_PROMPT = """你是危机复核器。判断用户最新一条消息本身是否表达自伤、自杀或绝望意图。

历史消息只用于理解语境；只有最新消息明确表达时才返回 true。

只输出 JSON：{"is_crisis": true} 或 {"is_crisis": false}"""


async def confirm_crisis(user_text: str, history: list[dict]) -> bool:
    """模型判危机后的二次确认。

    返回 False 表示最新消息本身没有危机含义（可降级为 elevated）；
    输出不合法或超时由调用方保守处理，继续按危机执行。
    """
    messages = [
        {"role": "system", "content": SECOND_CHECK_PROMPT},
        {"role": "user", "content": _build_input(user_text, history)},
    ]
    async with asyncio.timeout(ANALYZE_TIMEOUT_SECONDS):
        data = await complete_json(messages, temperature=0.0)

    value = data.get("is_crisis")
    if not isinstance(value, bool):
        raise ValueError("second check output invalid")
    return value


async def analyze_message(user_text: str, history: list[dict]) -> dict:
    """调模型做结构化判断。

    输出不合法时抛 ValueError，由调用方降级到规则版；
    超时由 asyncio.timeout 控制，不会拖住首 token。
    """
    messages = [
        {"role": "system", "content": ANALYZE_PROMPT},
        {"role": "user", "content": _build_input(user_text, history)},
    ]
    async with asyncio.timeout(ANALYZE_TIMEOUT_SECONDS):
        data = await complete_json(messages, temperature=0.1)

    intent = _enum(data.get("intent"), INTENTS)
    emotion = _enum(data.get("emotion"), EMOTIONS)
    risk_level = _enum(data.get("risk_level"), RISK_LEVELS)
    if intent is None or emotion is None or risk_level is None:
        # 任一字段非法就整次作废，不拼接半模型半规则的结果
        raise ValueError("analyze output has invalid enum")

    return {
        "intent": intent,
        "emotion": emotion,
        "risk_level": risk_level,
        "queries": clean_queries(data.get("queries"), user_text),
        "followup_hint": _clean_followups(data.get("followup_hint")),
    }
