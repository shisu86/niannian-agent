"""单用户当前会话线程。

主动关怀需要知道用户正在哪个 thread 聊天，才能把通知写回会话。
单用户单进程，放内存即可；重启后前端会重新注册。
"""

_current_thread: str | None = None


def set_current_thread(thread_id: str) -> None:
    global _current_thread
    _current_thread = thread_id


def get_current_thread() -> str | None:
    return _current_thread


def clear_current_thread(thread_id: str) -> None:
    """删除会话时，如果它是当前会话，一并清空注册。"""
    global _current_thread
    if _current_thread == thread_id:
        _current_thread = None
