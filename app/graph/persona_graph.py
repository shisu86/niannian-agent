"""人设提取子图：extract → confirm → save。

为什么把 interrupt 单独放一个节点：
LangGraph 恢复中断时会把中断所在的节点从头重跑一遍。如果把 LLM 调用
放在 interrupt() 前面，用户确认时会再调一次模型，候选还可能前后不一致。
拆开后 resume 只重跑 confirm，interrupt() 直接返回用户提交的内容。
"""

import logging
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.services.llm import complete_json
from app.services.persona import (
    DEFAULT_PERSONA,
    build_fallback_prompt,
    create_persona,
    update_persona,
)

logger = logging.getLogger(__name__)

# 拼给模型的对话预算：太长会挤爆上下文，太短又看不出说话方式
MAX_DIALOG_CHARS = 6000

EXTRACT_PROMPT = """你是人格提取器。下面是一段对话，[目标] 标出的是需要模仿的人。

根据对话提取这个人的说话方式与性格，输出 JSON：
{"name":"称呼","relation":"与用户的关系","traits":["性格标签"],
 "catchphrases":["口头禅"],"values":["行为准则"],
 "care_style":"一句话的关心方式","system_prompt":"可直接用于对话的人格指令"}

要求：
1. name / relation 参考提示；没有依据就留空。
2. traits 3-6 个短标签，catchphrases 0-6 条，values 1-4 条。
3. catchphrases 必须来自对话原文，不要自己编。
4. care_style 一句话，说明这个人怎么表达关心。
5. system_prompt 200-500 字，写清楚称呼、语气、说话习惯、关心方式、不要做什么。
6. 只根据对话推断，不要编造。

只输出 JSON，不要任何解释。"""


class PersonaState(TypedDict, total=False):
    """人设子图的状态。"""

    messages: list[dict]  # [{text, is_target}]
    name_hint: str
    relation_hint: str
    candidates: dict  # 模型抽取后、等待确认的候选
    persona: dict  # interrupt 恢复后写入，save 节点落库
    persona_id: int | None  # 为空表示新建角色，有值表示更新


def _format_dialog(messages: list[dict]) -> str:
    """按字符预算保留最近对话；越靠后的内容通常越能体现说话方式。"""
    lines: list[str] = []
    budget = MAX_DIALOG_CHARS
    for item in reversed(messages):
        text = str(item.get("text") or "")
        prefix = "[目标] " if item.get("is_target") else "[对方] "
        # 先扣前缀预算，再截正文：不能把说话人标记一起截掉
        budget -= len(prefix)
        if budget <= 0:
            break
        if len(text) > budget:
            text = text[-budget:]
        lines.append(prefix + text)
        budget -= len(text)
        if budget <= 0:
            break
    lines.reverse()
    return "\n".join(lines)


def _normalize_candidates(data: dict, name_hint: str, relation_hint: str) -> dict:
    """校验并截断模型输出，字段不合法就退回提示或默认值。"""

    def text(key: str, limit: int) -> str:
        value = data.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    def items(key: str, max_items: int, max_len: int) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value[:max_items]:
            if isinstance(item, str) and item.strip():
                result.append(item.strip()[:max_len])
        return result

    name = text("name", 20) or name_hint.strip()[:20] or DEFAULT_PERSONA["name"]
    relation = text("relation", 20) or relation_hint.strip()[:20] or DEFAULT_PERSONA["relation"]
    traits = items("traits", 6, 20)
    catchphrases = items("catchphrases", 6, 20)
    values = items("values", 4, 40)
    care_style = text("care_style", 200)
    system_prompt = text("system_prompt", 4000)

    if not system_prompt:
        # 模型偷懒时不能让聊天没有 prompt
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


async def extract(state: PersonaState) -> dict:
    """调模型抽取候选人格；不写库，只写 state。"""
    messages = state.get("messages") or []
    name_hint = state.get("name_hint") or ""
    relation_hint = state.get("relation_hint") or ""

    user_content = (
        f"称呼提示：{name_hint or '（未提供）'}\n"
        f"关系提示：{relation_hint or '（未提供）'}\n\n"
        f"对话如下：\n{_format_dialog(messages)}\n\n请输出 JSON。"
    )
    data = await complete_json(
        [
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": user_content},
        ]
    )
    candidates = _normalize_candidates(data, name_hint, relation_hint)
    logger.info("persona extract done traits=%s", len(candidates["traits"]))
    return {"candidates": candidates}


async def confirm(state: PersonaState) -> dict:
    """暂停等用户确认。

    resume 时本节点会从头执行，但 interrupt() 会直接返回提交内容，
    不会重复调用模型。
    """
    decision = interrupt(state.get("candidates") or {})
    if not isinstance(decision, dict):
        decision = state.get("candidates") or {}

    if isinstance(decision.get("persona"), dict):
        # 新格式：{persona, persona_id}，persona_id 为空表示新建角色
        return {
            "persona": decision["persona"],
            "persona_id": decision.get("persona_id"),
        }
    return {"persona": decision}


async def save(state: PersonaState) -> dict:
    """把确认后的人设写进 persona 表：带 id 更新，否则新建角色。"""
    persona = state.get("persona") or state.get("candidates") or {}
    persona_id = state.get("persona_id")

    if persona_id:
        saved = await update_persona(persona_id, persona)
        if saved is None:
            saved = await create_persona(persona)
    else:
        saved = await create_persona(persona)

    logger.info("persona saved id=%s version=%s", saved.get("id"), saved.get("version"))
    return {"persona": saved}


def build_persona_graph(checkpointer: BaseCheckpointSaver):
    """编译人设子图；checkpointer 与主图共用同一个实例。"""
    graph = StateGraph(PersonaState)
    graph.add_node("extract", extract)
    graph.add_node("confirm", confirm)
    graph.add_node("save", save)

    graph.add_edge(START, "extract")
    graph.add_edge("extract", "confirm")
    graph.add_edge("confirm", "save")
    graph.add_edge("save", END)
    return graph.compile(checkpointer=checkpointer)
