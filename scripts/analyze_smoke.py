"""S6 分析冒烟（零成本，用假模型）。

验证：
1. 合法 JSON 能正确解析字段；
2. 非法枚举整次作废，交给调用方降级；
3. queries 去重、限长、限条数，全空时用原句兜底；
4. 模型超时能被 asyncio.timeout 截断；
5. nodes.analyze 在模型失败时降级规则版；
6. 危机词短路，不调用模型；
7. elevated 风险走共情分支。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/analyze_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="niannian-analyze-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from langchain_core.messages import HumanMessage  # noqa: E402

import app.services.analyze as analyze_module  # noqa: E402
from app.graph import nodes  # noqa: E402
from app.graph.routes import route_after_analyze  # noqa: E402

_responses: list = []
failures: list[str] = []


async def fake_complete_json(messages, **_kwargs):
    result = _responses.pop(0)
    if isinstance(result, Exception):
        raise result
    return result


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


BASE_RESULT = {
    "intent": "chat",
    "emotion": "calm",
    "risk_level": "normal",
    "queries": ["原句"],
    "followup_hint": [],
}


async def case_valid() -> None:
    _responses.append(
        {
            "intent": "vent",
            "emotion": "low",
            "risk_level": "normal",
            "queries": ["胃病 不能吃辣", "胃不舒服"],
            "followup_hint": [{"content": "下周复查胃镜", "due_at": None}],
        }
    )
    result = await analyze_module.analyze_message("我最近胃病犯了", [])
    check("valid enums", result["intent"] == "vent" and result["emotion"] == "low")
    check("queries kept", result["queries"] == ["胃病 不能吃辣", "胃不舒服"])
    check("followups kept", len(result["followup_hint"]) == 1)


async def case_invalid_enum() -> None:
    _responses.append({**BASE_RESULT, "risk_level": "panic"})
    try:
        await analyze_module.analyze_message("随便说说", [])
    except ValueError:
        check("invalid enum raises", True)
    else:
        check("invalid enum raises", False)


async def case_query_cleaning() -> None:
    _responses.append({**BASE_RESULT, "queries": ["a", "a", "b", "c", "d"]})
    result = await analyze_module.analyze_message("原句", [])
    check("queries max 3", len(result["queries"]) == 3, str(result["queries"]))
    check("queries dedup", result["queries"] == ["a", "b", "c"], str(result["queries"]))

    # 单独一条超长 query，确保截断逻辑真的被执行
    _responses.append({**BASE_RESULT, "queries": ["x" * 80]})
    result = await analyze_module.analyze_message("原句", [])
    check(
        "query truncated",
        len(result["queries"][0]) == analyze_module.MAX_QUERY_LEN,
        str(len(result["queries"][0])),
    )


async def case_queries_non_list() -> None:
    _responses.append({**BASE_RESULT, "queries": "胃病"})
    result = await analyze_module.analyze_message("我胃疼", [])
    check("queries non-list fallback", result["queries"] == ["我胃疼"], str(result["queries"]))


async def case_query_fallback() -> None:
    _responses.append({**BASE_RESULT, "queries": []})
    result = await analyze_module.analyze_message("我最近失眠", [])
    check("queries fallback", result["queries"] == ["我最近失眠"], str(result["queries"]))


async def case_timeout() -> None:
    async def slow(messages, **_kwargs):
        await asyncio.sleep(1.0)
        return BASE_RESULT

    original_timeout = analyze_module.ANALYZE_TIMEOUT_SECONDS
    original_complete = analyze_module.complete_json
    analyze_module.ANALYZE_TIMEOUT_SECONDS = 0.05
    analyze_module.complete_json = slow
    try:
        await analyze_module.analyze_message("随便说说", [])
    except TimeoutError:
        check("timeout raises", True)
    else:
        check("timeout raises", False)
    finally:
        analyze_module.ANALYZE_TIMEOUT_SECONDS = original_timeout
        analyze_module.complete_json = original_complete


async def case_route() -> None:
    check(
        "elevated goes empathy",
        route_after_analyze({"risk_level": "elevated", "emotion": "calm", "intent": "chat"})
        == "empathy",
    )
    check(
        "crisis goes safety",
        route_after_analyze({"risk_level": "crisis", "emotion": "low", "intent": "chat"})
        == "safety",
    )


async def case_llm_path() -> None:
    async def fake(user_text, history):
        return {
            "intent": "vent",
            "emotion": "low",
            "risk_level": "normal",
            "queries": ["胃病"],
            "followup_hint": [],
        }

    nodes.analyze_message = fake
    state = {"messages": [HumanMessage(content="今天被领导批了，感觉挺没劲的")], "trace": {}}
    result = await nodes.analyze(state)
    trace = result["trace"]["analyze"]
    check("llm mode", trace["mode"] == "llm", str(trace))
    check("llm queries", result["queries"] == ["胃病"], str(result["queries"]))


async def case_llm_crisis_node() -> None:
    """模型自己判出 crisis 时，节点层也要按危机处理。"""

    async def fake(user_text, history):
        return {
            "intent": "chat",
            "emotion": "low",
            "risk_level": "crisis",
            "queries": ["消失"],
            "followup_hint": [],
        }

    nodes.analyze_message = fake
    state = {"messages": [HumanMessage(content="我想消失")], "trace": {}}
    result = await nodes.analyze(state)
    check("llm crisis node", result["risk_level"] == "crisis")
    check("llm crisis route", route_after_analyze(result) == "safety")


async def case_node_timeout_fallback() -> None:
    """节点层要能兜住 analyze 的超时。"""

    async def slow(user_text, history):
        raise TimeoutError

    nodes.analyze_message = slow
    state = {"messages": [HumanMessage(content="今天天气不错")], "trace": {}}
    result = await nodes.analyze(state)
    check(
        "node timeout fallback",
        result["trace"]["analyze"]["mode"] == "rule_fallback",
        str(result["trace"]),
    )


async def case_fallback() -> None:
    async def boom(user_text, history):
        raise RuntimeError("boom")

    nodes.analyze_message = boom
    state = {"messages": [HumanMessage(content="今天天气不错")], "trace": {}}
    result = await nodes.analyze(state)
    trace = result["trace"]["analyze"]
    check("fallback mode", trace["mode"] == "rule_fallback", str(trace))
    check("fallback emotion", result["emotion"] == "calm", str(result["emotion"]))


async def case_crisis_short_circuit() -> None:
    called = {"value": False}

    async def spy(user_text, history):
        called["value"] = True
        return {**BASE_RESULT}

    nodes.analyze_message = spy
    state = {"messages": [HumanMessage(content="我不想活了")], "trace": {}}
    result = await nodes.analyze(state)
    check("crisis skips llm", called["value"] is False)
    check("crisis mode", result["trace"]["analyze"]["mode"] == "rule_crisis")
    check("crisis risk", result["risk_level"] == "crisis")


async def main() -> None:
    analyze_module.complete_json = fake_complete_json
    try:
        await case_valid()
        await case_invalid_enum()
        await case_query_cleaning()
        await case_queries_non_list()
        await case_query_fallback()
        await case_timeout()
        await case_route()
        await case_llm_path()
        await case_llm_crisis_node()
        await case_node_timeout_fallback()
        await case_fallback()
        await case_crisis_short_circuit()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
