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

    @field_validator("thread_id")
    @classmethod
    def _reject_persona_prefix(cls, value: str) -> str:
        """p- 前缀保留给人设子图，避免两图共用 thread_id 互相污染状态。"""
        if value.startswith("p-"):
            raise ValueError("thread_id 不能使用 p- 前缀")
        return value


class MessageItem(BaseModel):
    """人设提取用的一条对话；is_target 标记是否为要模仿的人。"""

    text: str = Field(min_length=1, max_length=2000)
    is_target: bool = False


class PersonaExtractRequest(BaseModel):
    """人设提取请求：一段对话 + 可选的称呼/关系提示。"""

    messages: list[MessageItem] = Field(min_length=1, max_length=500)
    name_hint: str = Field(default="", max_length=20)
    relation_hint: str = Field(default="", max_length=20)


class PersonaPayload(BaseModel):
    """人设内容。values 是对外字段，落库写 values_json。"""

    name: str = Field(default="", max_length=20)
    relation: str = Field(default="", max_length=20)
    traits: list[str] = Field(default_factory=list, max_length=10)
    catchphrases: list[str] = Field(default_factory=list, max_length=10)
    values: list[str] = Field(default_factory=list, max_length=10)
    care_style: str = Field(default="", max_length=200)
    system_prompt: str = Field(default="", max_length=4000)


class PersonaConfirmRequest(BaseModel):
    """确认人设：persona_thread_id 由 extract 接口返回。"""

    persona_thread_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^p-[A-Za-z0-9]{4,32}$",
    )
    persona: PersonaPayload
