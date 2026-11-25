"""通用响应模型。

除 SSE 与音频流外，所有接口都返回同一层包络：
{"ok": bool, "data": ..., "error": ..., "trace_id": "..."}
这样前端只需要写一套解析逻辑，不用为每个接口单独判断。
"""

from pydantic import BaseModel


class ApiResponse[T](BaseModel):
    """统一响应包络。

    data 的具体类型由调用方通过泛型指定，例如：
        response_model=ApiResponse[HealthData]
    成功时 error 为 None；失败时 ok=False、data=None，错误细节放 error。
    """

    ok: bool = True
    data: T | None = None
    error: dict | None = None  # {"code": "...", "message": "..."}
    trace_id: str = "-"  # 请求级追踪 ID，便于按 ID 查日志


class HealthData(BaseModel):
    """健康检查返回体。

    status 固定为 "ok"；db 反映数据库可用性，异常时降级为 "error"；
    version 用于部署时确认线上版本。
    """

    status: str
    db: str
    version: str
