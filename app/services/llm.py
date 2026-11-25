"""DeepSeek 调用封装。

统一处理：超时、重试、流式输出、错误映射。
密钥只从配置读取，不进入日志；日志里也不记录用户消息原文。

注意：client 是进程级单例。单 worker + 单事件循环下可以安全复用；
如果将来改成多事件循环，需要改成按循环创建。
"""

import logging
from collections.abc import AsyncIterator

from openai import APIError, APITimeoutError, AsyncOpenAI

from app.config import get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """懒加载模型客户端；缺 Key 时给出明确错误，而不是等到超时。"""
    global _client
    settings = get_settings()
    if not settings.deepseek_api_key:
        raise AppError("LLM_ERROR", "服务端未配置模型密钥", status=502)
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,  # SDK 自带指数退避重试
        )
    return _client


async def stream_chat(
    messages: list[dict],
    temperature: float = 0.8,
) -> AsyncIterator[str]:
    """流式生成对话，逐段 yield 文本增量。

    调用方负责累积完整文本并写入 state。
    失败时抛 AppError，由 SSE 层转换成流内 error 事件。
    """
    settings = get_settings()
    try:
        client = _get_client()
        stream = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=messages,
            stream=True,
            temperature=temperature,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except APITimeoutError as exc:
        logger.warning("llm timeout")
        raise AppError("LLM_TIMEOUT", "模型响应超时，请稍后重试", status=504) from exc
    except APIError as exc:
        # 只记录错误类型与状态码，不记录用户内容
        logger.error("llm api error: %s", type(exc).__name__)
        raise AppError("LLM_ERROR", "模型返回错误，请稍后重试", status=502) from exc
    except AppError:
        raise
    except Exception as exc:
        logger.exception("llm stream failed")
        raise AppError("LLM_ERROR", "模型调用失败，请稍后重试", status=502) from exc
