"""FastAPI 入口。

启动顺序：日志 → 配置 → 数据库初始化 → 对外服务；
关闭由 lifespan 统一管理，避免资源泄漏。
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import chat, persona, system
from app.api.proactive import router as proactive_router
from app.api.tts import router as tts_router
from app.config import BASE_DIR, get_settings
from app.db import init_db
from app.errors import error_body, register_exception_handlers
from app.graph.checkpointer import open_checkpointer
from app.graph.main_graph import build_graph
from app.graph.persona_graph import build_persona_graph
from app.logging import setup_logging
from app.services.proactive import tick as proactive_tick
from app.services.search import warmup

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时初始化日志与数据库。

    用 lifespan 而不是 @app.on_event，是因为新版 FastAPI 推荐写法，
    且能保证启动/关闭逻辑成对执行、异常可感知。
    """
    setup_logging()
    settings = get_settings()
    if not settings.deepseek_api_key:
        # S0 不依赖模型，缺 Key 只警告；S2 接入 LLM 时再强制校验
        logger.warning("未配置 DEEPSEEK_API_KEY，流式对话暂不可用（S0 阶段可忽略）")
    await init_db()  # 建表失败会直接阻断启动，避免带着坏数据库对外服务
    await warmup()  # 预热 jieba，避免首个聊天请求卡在词典加载上

    # checkpointer 的生命周期必须覆盖整个服务运行期：
    # 图在启动时编译一次，所有请求复用同一个实例，禁止每请求新建。
    checkpoint_path = Path(settings.checkpoint_db_path)
    if not checkpoint_path.is_absolute():
        checkpoint_path = BASE_DIR / checkpoint_path
    async with open_checkpointer(checkpoint_path) as saver:
        app.state.checkpointer = saver  # 清理过期人设线程时需要直接删除 checkpoint
        app.state.graph = build_graph(saver)
        # 人设子图与主图共用 checkpointer，但 thread_id 前缀不同，状态互不干扰
        app.state.persona_graph = build_persona_graph(saver)

        # 主动关怀：每 5 分钟扫描一次到期事项；tick 内部已兜住所有异常
        scheduler = AsyncIOScheduler(timezone=settings.tz)
        scheduler.add_job(proactive_tick, "interval", minutes=5, id="proactive")
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            # 关闭前等待后台图任务结束，避免向已关闭的 checkpointer 写入
            await chat.wait_background_tasks()


def create_app() -> FastAPI:
    """构建应用实例；独立成函数便于测试时创建隔离实例。"""
    settings = get_settings()
    app = FastAPI(
        title="念念 · AI 情感陪伴 Agent",
        version=settings.app_version,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next) -> Response:
        """为每个请求生成短 trace_id，写入 request.state 与响应头。

        同时在这里兜住所有未捕获异常：
        异常会从内层向外抛，若交给最外层 ServerErrorMiddleware 处理，
        响应就不会再经过本中间件与 CORS 中间件，导致 500 缺少
        X-Trace-Id 和跨域头。提前在这里转成统一包络，保证头不丢。
        """
        request.state.trace_id = uuid.uuid4().hex[:8]
        try:
            response: Response = await call_next(request)
        except Exception:
            # 堆栈只进日志，响应里只给统一错误结构
            logger.exception("unhandled error trace_id=%s", request.state.trace_id)
            response = JSONResponse(
                error_body("INTERNAL", "服务器内部错误", request.state.trace_id),
                status_code=500,
            )
        response.headers["X-Trace-Id"] = request.state.trace_id
        # 开发期：所有非 API 响应不缓存（含 /、/index.html、JS、CSS），API/SSE 不受影响
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # 注意中间件顺序：后添加的 CORS 位于 trace_id 外层。
    # trace_id 内部已兜住异常，因此正常响应与 500 都会经过 CORS 加头。
    app.add_middleware(
        CORSMiddleware,
        # 开发期前后端分端口才需要 CORS；生产由 Caddy 同域反代，无需跨域
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # 允许前端读取 trace_id，排障时可直接展示
        expose_headers=["X-Trace-Id"],
    )

    register_exception_handlers(app)
    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(persona.router)
    app.include_router(proactive_router)
    app.include_router(tts_router)

    # 静态目录挂载必须放在所有 /api 路由之后，否则 "/" 会吞掉接口请求。
    # frontend 目录在 S2 才创建，这里先做存在性判断，避免启动报错。
    frontend_dir = BASE_DIR / "frontend"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


app = create_app()
