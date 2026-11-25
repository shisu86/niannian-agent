"""通用响应模型。

除 SSE 与音频流外，所有接口都返回同一层包络：
{"ok": bool, "data": ..., "error": ..., "trace_id": "..."}
这样前端只需要写一套解析逻辑，不用为每个接口单独判断。
"""

from pydantic import BaseModel, Field, field_validator


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


class ChatRequest(BaseModel):
    """对话请求。thread_id 由前端生成并保存在 localStorage。"""

    # 限定字符集，避免任意字符串成为 checkpoint 键
    thread_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
    )
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        """去掉首尾空白与零宽字符；空内容直接拒绝，避免空轮次调用模型。"""
        cleaned = value.strip().strip("\u200b\u200c\u200d\ufeff")
        if not cleaned:
            raise ValueError("message 不能为空")
        return cleaned
