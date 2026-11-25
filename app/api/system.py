"""系统接口：健康检查。

用途有两个：
1. 本地或容器启动后，确认服务与数据库都可用；
2. 部署与监控的探活入口。
"""

import logging

from fastapi import APIRouter, Request

from app.config import get_settings
from app.db import connect
from app.errors import get_trace_id
from app.models import ApiResponse, HealthData

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=ApiResponse[HealthData])
async def health(request: Request) -> ApiResponse[HealthData]:
    """返回服务与数据库状态。

    这里不用 Depends(get_db)：依赖会在进入函数前建连，
    一旦建连失败，函数根本没机会返回降级结果。
    健康检查的职责恰恰是报告"数据库坏了"，所以自己建连、自己兜住异常。

    response_model 会按 ApiResponse[HealthData] 做序列化校验，
    避免路由里返回了多余字段却悄悄漏到前端。
    """
    db_status = "ok"
    conn = None
    try:
        conn = await connect()
        # 只验证连接可用；不查业务表，避免健康检查依赖具体表结构
        await conn.execute("SELECT 1")
    except Exception:
        # 捕获必须记日志，否则线上只能看到 db=error，无法定位原因
        logger.exception("health db check failed")
        db_status = "error"
    finally:
        if conn is not None:
            await conn.close()

    return ApiResponse(
        data=HealthData(status="ok", db=db_status, version=get_settings().app_version),
        trace_id=get_trace_id(request),
    )
