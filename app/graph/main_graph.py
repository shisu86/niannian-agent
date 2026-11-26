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
    reflect,
    retrieve,
)
from app.graph.routes import route_after_analyze
from app.graph.state import AgentState
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
    graph.add_node("reflect", reflect)

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
    graph.add_edge("generate", "reflect")
    graph.add_edge("reflect", END)

    return graph.compile(checkpointer=checkpointer)


async def build_initial_state(user_text: str) -> dict:
    """组装一轮对话的初始 state。

    S2 注入用户消息与当前人设；time_context 留给 S7。
    这里不发 LLM 请求，只做数据准备。
    """
    persona = await get_persona()
    return {
        "messages": [HumanMessage(content=user_text)],
        "persona": persona,
        "trace": {},
    }
