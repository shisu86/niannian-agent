"""S1 占位节点。

本阶段不调用 LLM，用关键词规则模拟 analyze 的结果，
目的是验证图结构、条件边与状态持久化。

S2 起 generate 接入 DeepSeek 流式输出；
S6 起 analyze 换成一次结构化 LLM 调用（意图/情绪/风险/query），
届时本文件的规则只作为极端输入的兜底。
"""

import logging
import re

from langchain_core.messages import AIMessage

from app.graph.state import AgentState

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

# 匹配前去掉空白（含换行）与零宽字符，防止“想 死”这类变形绕过。
# 用显式转义而不是直接写不可见字符，避免源码里出现看不见的字符。
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


async def analyze(state: AgentState) -> dict:
    """规则版 analyze：一次产出风险、情绪、意图。

    S6 会替换为单次 LLM 结构化调用，同时产出 queries 与 followup_hint。
    规则版仍然保留，作为模型不可用时的安全检查兜底。
    """
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
    """四个 compose 分支的公共逻辑。

    S1 只记录分支与指令；S2 起这里会真正组装 system + 记忆 + 窗口消息。
    """
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


async def generate(state: AgentState) -> dict:
    """S1 占位回复：不调用模型，只回显分支与输入。

    回显限制在 50 字以内；S2 会被真实流式生成替换。
    """
    raw = _last_user_text(state)
    preview = raw if len(raw) <= 50 else raw[:50] + "…"
    branch = state.get("branch", "normal")
    reply = f"【S1 骨架】当前分支：{branch}。收到：{preview}"
    return {
        "messages": [AIMessage(content=reply)],
        "trace": _update_trace(state, generate={"rule_based": True}),
    }


async def reflect(state: AgentState) -> dict:
    """S1 不写记忆，只记录一次；S4 改成异步抽取并落库。"""
    logger.info("reflect skipped (S1) branch=%s", state.get("branch"))
    return {"trace": _update_trace(state, reflect="skipped_in_s1")}
