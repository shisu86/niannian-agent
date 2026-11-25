"""主图节点。

S2 起 generate 接入 DeepSeek 流式输出：
- 安全分支不调用模型，直接返回固定求助话术；
- 其余分支组装 system + 最近 8 轮窗口，调用模型并逐 token 写出。

S6 会把 analyze 换成结构化 LLM 调用；当前规则版作为极端输入的安全兜底。
"""

import logging
import re

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from app.graph.state import AgentState
from app.services.llm import stream_chat

logger = logging.getLogger(__name__)

# S1 的临时规则，仅用于让条件边可复现；S6 会被模型替换。
# 危机词尽量使用完整表达（如“想死”而非“死”），避免“累死我了”这类误判；
# “没意思”单独出现太常见，必须和“活着”共现才算危机。
CRISIS_WORDS = (
    "自杀",
    "轻生",
    "自残",
    "想不开",
    "活不下去",
    "不想活",
    "想死",
    "撑不住",
    "活着没意思",
    "活着没意义",
)
ANXIOUS_WORDS = ("焦虑", "紧张", "睡不着", "心慌")
# 使用组合词而不是单个“累”“烦”，避免命中“积累”“麻烦”这类无关词
LOW_WORDS = (
    "好累",
    "很累",
    "太累",
    "特别累",
    "累死",
    "疲惫",
    "难过",
    "心烦",
    "好烦",
    "委屈",
    "压力大",
)
HELP_WORDS = ("怎么办", "帮我", "该不该", "建议")

# 安全分支的固定话术：不经过模型，避免模型擅继续角色扮演
SAFETY_REPLY = (
    "听到你这么说，我很担心。我是 AI，不能替代真正的帮助，但这句话我必须认真回应："
    "如果你现在有伤害自己的想法，请先联系下面任何一个人。\n\n"
    "全国心理援助热线：12356\n"
    "北京心理危机研究与干预中心：010-82951332\n"
    "紧急情况：120\n\n"
    "我会在这里陪你，但请先照顾好自己。"
)

# 匹配前去掉空白（含换行）与零宽字符，防止“想 死”这类变形绕过。
_IGNORED_CHARS = re.compile(r"[\s\u200b-\u200f\u2028-\u202f\ufeff]+")


def _normalize(text: str) -> str:
    """归一化用户输入：去掉空白、换行与零宽字符。"""
    return _IGNORED_CHARS.sub("", text)


def _last_user_text(state: AgentState) -> str:
    """取最近一条用户消息的原文；没有或类型异常时返回空串。"""
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", "") != "human":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        # 多模态内容可能是块列表，避免把 Python repr 回显给用户
        return "" if content is None else str(content)
    return ""


def _update_trace(state: AgentState, **fields) -> dict:
    """复制并补充 trace，避免直接修改原 dict。"""
    trace = dict(state.get("trace") or {})
    trace.update(fields)
    return trace


def _get_writer():
    """获取 LangGraph 的自定义流写入器。

    非流式调用（脚本 ainvoke）时没有 writer，返回 None，
    节点仍然能正常生成完整回复。
    """
    try:
        return get_stream_writer()
    except Exception:
        return None


async def analyze(state: AgentState) -> dict:
    """规则版 analyze：一次产出风险、情绪、意图。"""
    raw = _last_user_text(state)
    text = _normalize(raw)  # 匹配用归一化文本，回显仍用原文

    risk_level = "crisis" if any(word in text for word in CRISIS_WORDS) else "normal"

    if risk_level == "crisis":
        # 危机表达统一按低落处理，避免 trace 里出现 crisis + calm 的矛盾组合
        emotion = "low"
    elif any(word in text for word in ANXIOUS_WORDS):
        emotion = "anxious"
    elif any(word in text for word in LOW_WORDS):
        emotion = "low"
    else:
        emotion = "calm"

    intent = "help" if any(word in text for word in HELP_WORDS) else "chat"

    logger.info("analyze risk=%s emotion=%s intent=%s", risk_level, emotion, intent)
    return {
        "risk_level": risk_level,
        "emotion": emotion,
        "intent": intent,
        "queries": [],  # S6 由 LLM 生成事实/事件/情感多条 query
        "followup_hint": [],  # S7 由 LLM 抽取待跟进事项
        "trace": _update_trace(
            state,
            analyze={"rule_based": True, "length": len(raw)},
        ),
    }


def _compose(state: AgentState, branch: str, directive: str) -> dict:
    """四个 compose 分支的公共逻辑：记录分支与本轮指令。"""
    return {
        "branch": branch,
        "trace": _update_trace(state, branch=branch, directive=directive),
    }


async def compose_safety(state: AgentState) -> dict:
    """危机分支：停止角色扮演，优先给出求助渠道。"""
    return _compose(state, "safety", "停止角色扮演，优先提供求助渠道")


async def compose_empathy(state: AgentState) -> dict:
    """共情分支：先承接情绪，不急着给方案。"""
    return _compose(state, "empathy", "先共情，后建议，不主动给方案")


async def compose_advice(state: AgentState) -> dict:
    """建议分支：结构化给出可执行方案。"""
    return _compose(state, "advice", "结构化给出可执行建议")


async def compose_normal(state: AgentState) -> dict:
    """常规分支：自然对话，保持人设语气。"""
    return _compose(state, "normal", "自然对话，保持人设语气")


def _build_system_prompt(state: AgentState) -> str:
    """人设 prompt + 本轮分支指令。"""
    persona = state.get("persona") or {}
    base = persona.get("system_prompt") or "你是一个温柔、真诚的陪伴者。"
    directive = (state.get("trace") or {}).get("directive", "")
    return f"{base}\n\n本轮回应要求：{directive}" if directive else base


def _to_openai_messages(state: AgentState) -> list[dict]:
    """把最近 8 轮消息转成 OpenAI 格式。

    S2 先用固定条数窗口；S3 再加 token 预算与优先级裁剪。
    """
    history = list(state.get("messages", []))
    if len(history) > 16:
        history = history[-16:]
        # 按条数裁剪可能从一条 assistant 开始，而它对应的用户提问已被裁掉；
        # 丢掉开头的非用户消息，保证窗口从一条用户提问开始。
        while history and getattr(history[0], "type", "") != "human":
            history = history[1:]

    result: list[dict] = []
    for message in history:
        role = {"human": "user", "ai": "assistant"}.get(getattr(message, "type", ""))
        content = getattr(message, "content", "")
        if role and isinstance(content, str):
            result.append({"role": role, "content": content})
    return result


async def generate(state: AgentState) -> dict:
    """生成回复。

    安全分支不调用模型，直接输出固定话术；
    其余分支调用 DeepSeek 流式生成，并逐 token 写入自定义流。
    """
    branch = state.get("branch", "normal")
    writer = _get_writer()

    if branch == "safety":
        if writer:
            writer({"type": "token", "text": SAFETY_REPLY})
        return {
            "messages": [AIMessage(content=SAFETY_REPLY)],
            "trace": _update_trace(state, generate={"mode": "fixed_safety"}),
        }

    messages = [{"role": "system", "content": _build_system_prompt(state)}]
    messages.extend(_to_openai_messages(state))

    parts: list[str] = []
    async for token in stream_chat(messages):
        parts.append(token)
        if writer:
            writer({"type": "token", "text": token})

    content = "".join(parts).strip()
    if not content:
        content = "（我这边没有生成出内容，再和我说一次好吗？）"
        if writer:
            writer({"type": "token", "text": content})

    return {
        "messages": [AIMessage(content=content)],
        "trace": _update_trace(state, generate={"mode": "llm_stream"}),
    }


async def reflect(state: AgentState) -> dict:
    """S2 仍不写记忆，只记录一次；S4 改成异步抽取并落库。"""
    logger.info("reflect skipped (S2) branch=%s", state.get("branch"))
    return {"trace": _update_trace(state, reflect="skipped_in_s2")}
