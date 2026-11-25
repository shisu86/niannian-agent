"""LangGraph checkpointer 生命周期。

checkpointer 负责短期记忆（会话消息、图状态、interrupt 现场），
存于独立文件 checkpoints.db，与业务库 app.db 分开，避免写锁竞争。

必须在 FastAPI lifespan 中打开一次并全局复用；
每个请求新建会反复建连接、丢锁，是文档里明确列出的坑。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)


@asynccontextmanager
async def open_checkpointer(path: str | Path) -> AsyncIterator[AsyncSqliteSaver]:
    """打开 checkpoints.db，并在退出时自动关闭连接。"""
    db_path = Path(path)
    # mkdir 是微秒级操作，不值得为它切线程
    db_path.parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        # 显式建表，让路径或权限问题在启动阶段就暴露（fail-fast）。
        # 说明：3.1.3 的 aget_tuple/aput 内部也会调用 setup()，
        # 所以这里不是"不调用就会报 no such table"，而是把失败提前到启动期。
        # setup() 是幂等的（CREATE TABLE IF NOT EXISTS + is_setup 标记）。
        await saver.setup()
        logger.info("checkpointer ready: %s", db_path)
        yield saver
