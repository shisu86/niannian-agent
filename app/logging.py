"""日志配置。

统一输出格式，便于后续把 trace_id、thread_id、耗时写进日志。
注意：本模块虽然叫 logging，但 Python 3 默认绝对导入，
内部 `import logging` 拿到的是标准库，不会自我引用。
"""

import logging
import sys

# 给本模块添加的 handler 打标记，重复调用时只清理自己添加的，
# 避免把 pytest caplog 或其他组件挂在 root 上的 handler 一并清掉。
HANDLER_MARK = "niannian_handler"


def setup_logging(level: int = logging.INFO) -> None:
    """初始化 root logger。

    设计要点：
    1. 输出到 stdout，容器环境由 Docker 统一收集；
    2. 重复调用时只移除上一次本模块添加的 handler，避免 --reload 下日志重复；
    3. 只改 root logger，uvicorn 自身的 logger 配置不受影响。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    handler.niannian_handler = True  # type: ignore[attr-defined]

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if getattr(existing, HANDLER_MARK, False):
            root.removeHandler(existing)
    root.addHandler(handler)
