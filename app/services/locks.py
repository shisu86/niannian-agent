"""进程内按 thread 串行化写入。

聊天图任务和主动关怀的 aupdate_state 都会写同一个 checkpoint，
必须共用同一把锁，避免互相覆盖。
"""

import asyncio

_locks: dict[str, asyncio.Lock] = {}


def get_thread_lock(thread_id: str) -> asyncio.Lock:
    lock = _locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[thread_id] = lock
    return lock
