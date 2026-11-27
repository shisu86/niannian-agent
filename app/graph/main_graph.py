"""主图构建与初始状态组装。"""

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph.nodes import (
    analyze,
    compose_advice,
    compose_empathy,
    compose_normal,
    compose_safety,
    generate,
    retrieve,
)
from app.graph.routes import route_after_analyze
from app.graph.state import AgentState
from app.services.conversation import get_thread_persona
from app.services.persona import get_persona

# 四个 compose 分支同构，只差指令片段；统一连到 generate
COMPOSE_NODES = ("compose_safety", "compose_empathy", "compose_advice", "compose_normal")


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """构建并编译主图。

    checkpointer 由 lifespan 打开一次并复用，禁止每个请求新建。
    """
    graph = StateGraph(AgentState)
    graph.add_node("analyze", analyze)
    graph.add_node("retrieve", retrieve)
    graph.add_node("compose_safety", compose_safety)
    graph.add_node("compose_empathy", compose_empathy)
    graph.add_node("compose_advice", compose_advice)
    graph.add_node("compose_normal", compose_normal)
    graph.add_node("generate", generate)

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        route_after_analyze,
        {
            "safety": "compose_safety",
            "empathy": "compose_empathy",
            "advice": "compose_advice",
            "normal": "compose_normal",
        },
    )
    for name in COMPOSE_NODES:
        graph.add_edge(name, "generate")
    # 图在 generate 后立即结束：记忆抽取是图外异步任务，
    # 不写会话 checkpoint，避免慢速抽取覆盖下一轮对话。
    graph.add_edge("generate", END)

    return graph.compile(checkpointer=checkpointer)


async def build_initial_state(user_text: str, thread_id: str) -> dict:
    """组装一轮对话的初始 state。

    按会话绑定的角色取人设；没有绑定记录时退回默认角色 1。
    """
    persona_id = await get_thread_persona(thread_id)
    persona = await get_persona(persona_id)
    return {
        "messages": [HumanMessage(content=user_text)],
        "persona": persona,
        "persona_id": persona_id,
        "trace": {},
    }
