"""依赖注入。

每个请求单独获取一个 SQLite 连接，请求结束立即关闭。
单 worker + WAL 下，这个开销远小于维护连接池的复杂度。
"""

from collections.abc import AsyncIterator

import aiosqlite

from app.db import connect


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI 依赖：提供请求级连接，保证异常时也能关闭。

    yield 之前的代码在进入路由前执行，finally 在响应返回后执行，
    因此即使路由抛异常，连接也会被释放。
    """
    conn = await connect()
    try:
        yield conn
    finally:
        await conn.close()
