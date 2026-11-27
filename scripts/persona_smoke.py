"""S5 人设子图冒烟（零成本，用假模型 + 临时库）。

验证：
1. 默认人设幂等创建，is_default=1；
2. extract 后停在 interrupt，候选已写入 state；
3. Command(resume) 恢复后写库，version 自增；
4. 模型字段缺失时有兜底 system_prompt 与默认称呼；
5. save_persona 的清洗规则（截断、过滤非字符串）。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/persona_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须先设环境变量再导入 app.*，确保用临时库
_TMP_DIR = tempfile.mkdtemp(prefix="niannian-persona-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from langgraph.types import Command  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import app.graph.persona_graph as persona_graph_module  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.graph.checkpointer import open_checkpointer  # noqa: E402
from app.graph.persona_graph import _format_dialog, build_persona_graph  # noqa: E402
from app.models import ChatRequest  # noqa: E402
from app.services.persona import (  # noqa: E402
    build_fallback_prompt,
    ensure_persona,
    get_full_persona,
    save_persona,
)

_responses: list[dict] = []
failures: list[str] = []


async def fake_complete_json(messages, **_kwargs) -> dict:
    """替换子图里的模型调用，按顺序返回预置 JSON。"""
    return _responses.pop(0)


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


CANDIDATES_MOM = {
    "name": "妈妈",
    "relation": "母亲",
    "traits": ["刀子嘴豆腐心", "要强"],
    "catchphrases": ["哎呀", "你吃了吗"],
    "values": ["用唠叨表达关心"],
    "care_style": "用唠叨和追问表达关心",
    "system_prompt": "你是妈妈，说话直接，嘴上嫌弃但心里惦记。",
}

CANDIDATES_DAD = {
    "name": "爸爸",
    "relation": "父亲",
    "traits": ["沉默", "务实"],
    "catchphrases": ["嗯"],
    "values": ["少说多做"],
    "care_style": "用行动表达关心",
    "system_prompt": "你是爸爸，话不多，但会默默把事情做好。",
}


async def case_default_persona() -> None:
    await init_db()
    await ensure_persona()
    first = await get_full_persona()
    check("default persona created", bool(first) and first["version"] == 1)
    check(
        "default marked",
        bool(first) and first["is_default"] == 1,
        str(first["is_default"] if first else None),
    )

    await ensure_persona()
    second = await get_full_persona()
    check("default idempotent", second["version"] == first["version"])


async def case_interrupt_and_resume(graph) -> None:
    _responses.append(CANDIDATES_MOM)
    config = _config("p-smoke0001")
    await graph.ainvoke(
        {
            "messages": [{"text": "又不回来？行吧，你自己看着办。", "is_target": True}],
            "name_hint": "妈妈",
            "relation_hint": "母亲",
        },
        config,
    )

    snapshot = await graph.aget_state(config)
    check("paused at interrupt", bool(snapshot.next), str(snapshot.next))
    candidates = (snapshot.values or {}).get("candidates") or {}
    check("candidates extracted", candidates.get("name") == "妈妈", str(candidates.get("name")))

    # 模拟用户在确认弹窗里改了两处
    confirmed = dict(candidates)
    confirmed["traits"] = ["刀子嘴豆腐心", "要强", "爱操心"]
    await graph.ainvoke(Command(resume={"persona": confirmed, "persona_id": 1}), config)

    snapshot = await graph.aget_state(config)
    persona = (snapshot.values or {}).get("persona") or {}
    check("persona saved", persona.get("name") == "妈妈", str(persona.get("name")))
    check("version incremented", persona.get("version") == 2, str(persona.get("version")))
    check("user edits kept", "爱操心" in (persona.get("traits") or []))


async def case_second_persona(graph) -> None:
    _responses.append(CANDIDATES_DAD)
    config = _config("p-smoke0002")
    await graph.ainvoke(
        {
            "messages": [{"text": "嗯，知道了。", "is_target": True}],
            "name_hint": "爸爸",
            "relation_hint": "父亲",
        },
        config,
    )
    await graph.ainvoke(
        Command(resume={"persona": CANDIDATES_DAD, "persona_id": 1}),
        config,
    )

    snapshot = await graph.aget_state(config)
    persona = (snapshot.values or {}).get("persona") or {}
    check("second persona version", persona.get("version") == 3, str(persona.get("version")))
    check("second persona name", persona.get("name") == "爸爸", str(persona.get("name")))


async def case_fallback_fields(graph) -> None:
    _responses.append({})  # 模型什么都没给
    config = _config("p-smoke0003")
    await graph.ainvoke(
        {"messages": [{"text": "嗯。", "is_target": True}], "name_hint": "", "relation_hint": ""},
        config,
    )
    snapshot = await graph.aget_state(config)
    candidates = (snapshot.values or {}).get("candidates") or {}
    check("fallback name", candidates.get("name") == "念念", str(candidates.get("name")))
    check("fallback prompt", bool(candidates.get("system_prompt")))


async def case_save_cleaning() -> None:
    saved = await save_persona(
        {
            "name": "长" * 30,
            "traits": ["a", 1, "", "b"],
            "catchphrases": None,
            "values": ["v" * 100],
            "care_style": None,
            "system_prompt": "",
        }
    )
    check("name truncated", len(saved["name"]) == 20, str(len(saved["name"])))
    check("list cleaned", saved["traits"] == ["a", "b"], str(saved["traits"]))
    check("values truncated", len(saved["values"][0]) == 60, str(len(saved["values"][0])))
    check("empty prompt fallback", bool(saved["system_prompt"]))
    # 兜底 prompt 必须按本次字段生成，不能退回"念念"
    check("fallback uses current name", "长" in saved["system_prompt"])


def case_helpers() -> None:
    """纯函数与请求模型的边界检查。"""
    text = _format_dialog([{"text": "x" * 7000, "is_target": True}])
    check("dialog prefix kept", text.startswith("[目标] "), text[:12])
    check("dialog within budget", len(text) <= 6010, str(len(text)))

    prompt = build_fallback_prompt("妈妈", "母亲", ["嘴硬"], ["哎呀"], "唠叨")
    check("fallback prompt has name", "妈妈" in prompt)
    check("fallback prompt has traits", "嘴硬" in prompt)

    try:
        ChatRequest(thread_id="p-12345678", message="hi")
    except ValidationError:
        check("chat rejects p- prefix", True)
    else:
        check("chat rejects p- prefix", False)

    ChatRequest(thread_id="abcdefgh", message="hi")
    check("chat accepts normal id", True)


async def case_adelete_thread(graph) -> None:
    """过期清理依赖这个方法；版本不支持时需要提前知道。"""
    try:
        await graph.checkpointer.adelete_thread("p-not-exist")
        check("adelete_thread available", True)
    except AttributeError:
        check("adelete_thread available", False)


async def case_concurrent_save() -> None:
    """首次并发写入不应撞 UNIQUE，也不应抛异常。"""
    conn = await connect()
    try:
        await conn.execute("DELETE FROM persona")
        await conn.commit()
    finally:
        await conn.close()

    results = await asyncio.gather(
        save_persona({"name": "甲", "system_prompt": "你是甲"}),
        save_persona({"name": "乙", "system_prompt": "你是乙"}),
    )
    check("concurrent save ok", len(results) == 2, str([r["name"] for r in results]))


async def main() -> None:
    persona_graph_module.complete_json = fake_complete_json
    checkpoint_path = Path(_TMP_DIR) / "checkpoints.db"
    try:
        case_helpers()
        await case_default_persona()
        async with open_checkpointer(checkpoint_path) as saver:
            graph = build_persona_graph(saver)
            await case_adelete_thread(graph)
            await case_interrupt_and_resume(graph)
            await case_second_persona(graph)
            await case_fallback_fields(graph)
        await case_save_cleaning()
        await case_concurrent_save()
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
