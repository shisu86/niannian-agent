"""条件边路由函数。

路由只做判断，不产生副作用；所有分支选择都能在 trace 里看到。
"""

from app.graph.state import AgentState


def route_after_analyze(state: AgentState) -> str:
    """风险优先级最高：crisis > 情绪 > 求助 > 常规。

    顺序不能调换：危机表达即使同时包含低落情绪，也必须走安全分支。
    """
    if state.get("risk_level") == "crisis":
        return "safety"
    # elevated 是模型判定的中间风险：不走求助话术，但先共情
    if state.get("risk_level") == "elevated" or state.get("emotion") in ("low", "anxious"):
        return "empathy"
    if state.get("intent") == "help":
        return "advice"
    return "normal"
