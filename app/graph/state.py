"""主图的共享状态。

约束：
1. 所有值必须可 JSON 序列化（datetime 转 ISO 字符串），
   因为 checkpointer 会把整个 state 写入 SQLite。
2. messages 使用官方 add_messages 归约器，新消息追加而不是覆盖。
3. 短期窗口只在读取时裁剪，不改写 checkpoint。
"""

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """一轮对话在主图中流转的全部字段。"""

    # 短期消息窗口，由 checkpointer 按 thread_id 持久化
    messages: Annotated[list, add_messages]
    # 当前人设：结构化字段 + system_prompt
    persona: dict
    # 当前会话绑定的角色 id，用于检索与记忆写入
    persona_id: int
    # 意图：chat / vent / help / recall / farewell
    intent: str
    # 情绪：calm / low / anxious / happy
    emotion: str
    # 风险等级：normal / elevated / crisis（crisis 优先级最高）
    risk_level: str
    # analyze 规划出的多条检索 query（S6 由 LLM 生成）
    queries: list[str]
    # 本轮命中的长期记忆（S3/S4 填充）
    retrieved: list[dict]
    # analyze 识别出的待跟进线索，由 reflect 落库（S7）
    followup_hint: list[dict]
    # 本轮走向：safety / empathy / advice / normal
    branch: str
    # 时间情境：距上次对话时长、当前时段、节日
    time_context: dict
    # 本轮节点顺序、检索、预算、耗时（S9 侧栏使用）
    trace: dict
