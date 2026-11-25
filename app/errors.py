"""业务异常与全局异常处理。

目标：无论成功还是失败，非 SSE 接口都返回统一包络，
前端不需要针对 404 / 422 / 500 分别写解析分支。

注意：未捕获异常会先在 trace_id 中间件里被兜住（见 main.py），
这里的 Exception 处理器只作为最后一道保险。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常。

    code 对应开发文档错误码表；status 是 HTTP 状态码。
    统一由下方处理器转换成响应包络。
    """

    def __init__(self, code: str, message: str, status: int = 500) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def error_body(code: str, message: str, trace_id: str) -> dict:
    """构造失败响应体，字段与 ApiResponse 保持一致。

    用普通 dict 而不是模型，是为了让处理器不依赖 Pydantic 序列化开销。
    """
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message},
        "trace_id": trace_id,
    }


def get_trace_id(request: Request) -> str:
    """读取请求级 trace_id；中间件未执行时返回占位符。"""
    return getattr(request.state, "trace_id", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """注册四类异常处理：业务异常、HTTP 异常、参数校验、兜底异常。

    注册顺序不影响匹配，Starlette 按异常类型精确查找。
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # 业务异常属于预期内错误，只记 warning，不打堆栈
        logger.warning(
            "app_error trace_id=%s code=%s message=%s",
            get_trace_id(request),
            exc.code,
            exc.message,
        )
        return JSONResponse(
            error_body(exc.code, exc.message, get_trace_id(request)),
            status_code=exc.status,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 把 FastAPI/Starlette 默认的 {"detail": ...} 也包成统一结构。
        # 只映射文档错误码表里出现过的状态；其余给一个通用 HTTP_ERROR。
        if exc.status_code == 401:
            code = "UNAUTHORIZED"
        elif exc.status_code == 404:
            code = "NOT_FOUND"
        elif exc.status_code == 429:
            code = "RATE_LIMITED"
        elif exc.status_code >= 500:
            # 5xx 不把原始 detail 透给客户端，避免内部信息泄露
            code = "INTERNAL"
        else:
            code = "HTTP_ERROR"

        if exc.status_code >= 500:
            message = "服务器内部错误"
        else:
            # detail 可能是 dict（例如自定义 HTTPException），统一转成可读字符串
            message = exc.detail if isinstance(exc.detail, str) else "请求失败"

        # 必须转发原始响应头：401 的 WWW-Authenticate、429 的 Retry-After
        # 都是协议语义的一部分，重新构造响应时丢掉会破坏客户端行为。
        headers = dict(exc.headers) if exc.headers else None
        return JSONResponse(
            error_body(code, message, get_trace_id(request)),
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数错误统一映射为 400 + INVALID_ARGUMENT（文档约定，而非框架默认的 422）。
        # 只记录位置、类型、提示，不记录 input 原始值，避免用户私密输入进入日志。
        details = [
            {
                "loc": list(error.get("loc", [])),
                "type": error.get("type"),
                "msg": error.get("msg"),
            }
            for error in exc.errors()
        ]
        logger.warning("validation error trace_id=%s details=%s", get_trace_id(request), details)
        return JSONResponse(
            error_body("INVALID_ARGUMENT", "参数校验失败", get_trace_id(request)),
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 最后一道保险：完整堆栈只进日志，响应里不暴露内部细节
        logger.exception("unhandled error trace_id=%s", get_trace_id(request))
        return JSONResponse(
            error_body("INTERNAL", "服务器内部错误", get_trace_id(request)),
            status_code=500,
        )
