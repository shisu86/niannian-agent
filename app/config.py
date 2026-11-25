"""应用配置。

所有可调参数集中在这里，通过 pydantic-settings 读取。
优先级：环境变量 > .env 文件 > 类默认值。
密钥只允许走环境变量或本地 .env，绝不写进代码或仓库。

注意：get_settings() 带缓存，进程启动后再改 .env 不会生效，需要重启服务。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：app/config.py 的上两级。
# 所有相对路径都基于它解析，避免"从哪个目录启动"影响数据位置。
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """与 .env.example 一一对应的配置项。

    字段名自动映射为同名大写环境变量（大小写不敏感），
    因此 .env 里写 DEEPSEEK_API_KEY 即可填充 deepseek_api_key。
    """

    # 固定读取项目根目录的 .env；extra="ignore" 允许系统里存在无关变量
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    deepseek_api_key: str = ""  # 允许为空：S0 阶段不调用模型
    deepseek_base_url: str = "https://api.deepseek.com"  # OpenAI SDK 兼容地址
    deepseek_model: str = "deepseek-chat"  # 生成、抽取、评估共用同一模型
    llm_timeout_seconds: int = 30  # 单次调用超时，超时返回 LLM_TIMEOUT
    llm_max_retries: int = 1  # 额外重试次数，总尝试次数为 1 + N

    # --- 存储 ---
    app_db_path: str = "data/app.db"  # 业务库：persona / memory / notifications
    checkpoint_db_path: str = "data/checkpoints.db"  # LangGraph 状态库（S1 使用）

    # --- 运行 ---
    tz: str = "Asia/Shanghai"  # 仅用于展示，存储统一 UTC
    # 预留字段：认证逻辑在 S7/S11 实现，当前版本尚未启用 HTTP Basic
    access_password: str = ""
    rate_limit_per_minute: int = 20  # 单 IP 每分钟请求上限（S7 实现）
    daily_turns_limit: int = 50  # 单 IP 每日对话轮数上限（S7 实现）
    proactive_default_quiet: str = "22:00-08:00"  # 首次建库时写入 settings
    proactive_daily_limit: int = 1  # 首次建库时写入 settings
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"  # 逗号分隔

    app_version: str = "1.0.0"  # 健康检查与 /docs 中展示

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的 CORS 白名单拆成列表，空项自动剔除。

        Starlette 要求 allow_origins 是列表；直接传逗号串会被当成一个来源。
        """
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """全局单例：避免每个请求重复解析 .env。

    测试中如需切换配置，先调用 get_settings.cache_clear()。
    """
    return Settings()
