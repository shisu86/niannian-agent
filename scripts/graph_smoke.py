"""主图冒烟脚本（S2）。

用法（在项目根目录执行）：
    ./.venv/Scripts/python.exe scripts/graph_smoke.py

默认把模型调用替换成假流，不产生真实费用，只验证：
1. 各类输入走对分支（含危机词、空格绕过、礼貌求助、日常闲聊反例）；
2. 同一 thread_id 连续两轮，消息会累积（checkpointer 生效）；
3. 不同 thread_id 的消息互不串。

任何一项不符合预期都会抛出 SystemExit(1)，可直接作为回归检查。
"""

import asyncio
import sys
from pathlib import Path

# 允许直接以脚本方式运行：把项目根加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import app.graph.nodes as nodes  # noqa: E402
from app.config import BASE_DIR  # noqa: E402
from app.graph.checkpointer import open_checkpointer  # noqa: E402
from app.graph.main_graph import build_graph, build_initial_state  # noqa: E402


async def _fake_stream(messages):
    """冒烟用假模型：返回固定文本，避免真实 API 费用。"""
    yield "【冒烟】这是一条测试回复。"


# 在构图前替换模块级函数，generate 节点会使用这个假流
nodes.stream_chat = _fake_stream

# (thread_id, 输入, 期望分支)
CASES = [
    ("u-smoke-safety", "我最近真的撑不住了，感觉没什么意思", "safety"),
    ("u-smoke-empathy", "最近好累，心里有点难过", "empathy"),
    ("u-smoke-advice", "面试准备不好，我该怎么办", "advice"),
    ("u-smoke-normal", "今天天气不错", "normal"),
    # 审核补充的反例：危机词、空格绕过、礼貌求助、日常“没意思”
    ("u-smoke-crisis-word", "我想自杀", "safety"),
    ("u-smoke-spaced", "我想 死", "safety"),
    ("u-smoke-polite-help", "麻烦帮我定个闹钟", "advice"),
    ("u-smoke-movie", "这电影真没意思", "normal"),
]


def _cleanup(db_path: Path) -> None:
    """删除冒烟库及其 WAL/SHM 辅助文件。"""
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


async def main() -> None:
    db_path = BASE_DIR / "data" / "_graph_smoke.db"
    _cleanup(db_path)
    failures: list[str] = []

    try:
        # 复用生产路径：open_checkpointer 负责建目录、建连接、显式 setup
        async with open_checkpointer(db_path) as saver:
            graph = build_graph(saver)

            print("=== 分支路由 ===")
            for thread_id, text, expected in CASES:
                config = {"configurable": {"thread_id": thread_id}}
                result = await graph.ainvoke(await build_initial_state(text), config)
                branch = result.get("branch")
                status = "OK  " if branch == expected else "FAIL"
                print(f"{status} {thread_id:22s} expected={expected:8s} got={branch}")
                if branch != expected:
                    failures.append(thread_id)

            print("=== 持久化 ===")
            config = {"configurable": {"thread_id": "u-smoke-safety"}}
            await graph.ainvoke(await build_initial_state("谢谢"), config)
            snapshot = await graph.aget_state(config)
            count = len(snapshot.values["messages"])
            print("safety thread messages:", count)
            if count != 4:
                failures.append(f"persistence:{count}")

            print("=== 线程隔离 ===")
            other = await graph.aget_state({"configurable": {"thread_id": "u-smoke-empathy"}})
            other_count = len(other.values["messages"])
            print("empathy thread messages:", other_count)
            if other_count != 2:
                failures.append(f"isolation:{other_count}")
    finally:
        # 即使中途断言失败，也不留下临时库
        _cleanup(db_path)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
