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

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

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

    async def confirm_true(text, history):
        return True

    # 模型报危机会触发二次确认；这里固定为确认，避免真实调用
    nodes.confirm_crisis = confirm_true

    state = {"messages": [HumanMessage(content="我想消失")], "trace": {}}
    result = await nodes.analyze(state)
    check("llm crisis node", result["risk_level"] == "crisis")
    check("llm crisis route", route_after_analyze(result) == "safety")


async def case_short_greeting_guard() -> None:
    """只有明确问候才降级；短危机词、需要历史的短回答都不能降。"""

    received = {"history": None}

    async def fake_crisis(user_text, history):
        received["history"] = len(history)
        return {
            "intent": "vent",
            "emotion": "low",
            "risk_level": "crisis",
            "queries": ["危机"],
            "followup_hint": [{"content": "待跟进", "due_at": None}],
        }

    nodes.analyze_message = fake_crisis

    async def confirm_true(text, history):
        return True

    # 非问候的短句会走二次确认；默认确认危机，避免真实调用模型
    nodes.confirm_crisis = confirm_true

    state = {"messages": [HumanMessage(content="你好")], "trace": {}}
    result = await nodes.analyze(state)
    check("greeting downgraded", result["risk_level"] == "normal", str(result["risk_level"]))
    check(
        "greeting mode",
        result["trace"]["analyze"]["mode"] == "llm_guarded",
        str(result["trace"]),
    )
    check("greeting emotion reset", result["emotion"] == "calm", str(result["emotion"]))
    check("greeting intent reset", result["intent"] == "chat", str(result["intent"]))
    check("greeting queries only raw", result["queries"] == ["你好"], str(result["queries"]))
    check("greeting no followup", result["followup_hint"] == [])
    check("greeting history cleared", received["history"] == 0, str(received))

    # 规则词表没覆盖的短危机词，不能当寒暄降级
    state = {"messages": [HumanMessage(content="割腕")], "trace": {}}
    result = await nodes.analyze(state)
    check("short crisis kept", result["risk_level"] == "crisis", str(result["risk_level"]))

    # 短回答依赖上下文（助手刚问过风险），不能清历史、不能降级
    state = {
        "messages": [
            HumanMessage(content="你最近还好吗"),
            AIMessage(content="你现在有伤害自己的想法吗？"),
            HumanMessage(content="有"),
        ],
        "trace": {},
    }
    result = await nodes.analyze(state)
    check("short answer keeps history", received["history"] == 2, str(received))
    check("short answer not downgraded", result["risk_level"] == "crisis")


async def case_crisis_second_check() -> None:
    """模型报危机后的二次确认：否定降 elevated，确认/失败保持 crisis。"""

    async def fake_crisis(user_text, history):
        return {
            "intent": "vent",
            "emotion": "low",
            "risk_level": "crisis",
            "queries": ["危机"],
            "followup_hint": [],
        }

    nodes.analyze_message = fake_crisis

    async def confirm_false(text, history):
        return False

    nodes.confirm_crisis = confirm_false
    state = {"messages": [HumanMessage(content="我今天好多了")], "trace": {}}
    result = await nodes.analyze(state)
    check(
        "second check downgrades",
        result["risk_level"] == "elevated",
        str(result["risk_level"]),
    )
    check(
        "second check mode",
        result["trace"]["analyze"]["mode"] == "llm_rechecked",
        str(result["trace"]),
    )

    async def confirm_true(text, history):
        return True

    nodes.confirm_crisis = confirm_true
    state = {"messages": [HumanMessage(content="割腕")], "trace": {}}
    result = await nodes.analyze(state)
    check("second check keeps crisis", result["risk_level"] == "crisis")

    async def confirm_boom(text, history):
        raise RuntimeError("boom")

    nodes.confirm_crisis = confirm_boom
    state = {"messages": [HumanMessage(content="割腕")], "trace": {}}
    result = await nodes.analyze(state)
    check("second check failure keeps crisis", result["risk_level"] == "crisis")
    check(
        "second check failure mode",
        result["trace"]["analyze"]["mode"] == "llm_check_failed",
        str(result["trace"]),
    )


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
        await case_short_greeting_guard()
        await case_crisis_second_check()
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
