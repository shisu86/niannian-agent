"""DeepSeek 调用封装。

统一处理：超时、重试、流式输出、错误映射。
密钥只从配置读取，不进入日志；日志里也不记录用户消息原文。

注意：client 是进程级单例。单 worker + 单事件循环下可以安全复用；
如果将来改成多事件循环，需要改成按循环创建。
"""

import json
import logging
from collections.abc import AsyncIterator

from openai import APIError, APITimeoutError, AsyncOpenAI

from app.config import get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

# 后台 JSON 任务（记忆抽取）单独的超时，比对话更短
JSON_TIMEOUT_SECONDS = 12.0


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


def _parse_json_object(text: str) -> dict:
    """解析模型返回的 JSON，容忍 ```json 代码围栏。

    只接受 JSON 对象；数组、纯文本都视为失败，交给调用方重试。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # 去掉首行的 ```json（或 ```），再去掉结尾的 ```
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


async def complete_json(
    messages: list[dict],
    temperature: float = 0.2,
) -> dict:
    """非流式 JSON 输出，供记忆抽取等后台任务使用。

    与 stream_chat 共用客户端；超时更短，
    避免后台任务把一次请求拖到 30 秒。
    """
    settings = get_settings()
    try:
        # 后台任务不再叠加 SDK 级重试：reflect 外层已经重试一次，
        # 否则 12 秒超时会被放大成约 48 秒，拖长后台任务生命周期。
        client = _get_client().with_options(max_retries=0)
        response = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=messages,
            temperature=temperature,
            # DeepSeek 与 OpenAI 兼容：要求返回合法 JSON 对象
            response_format={"type": "json_object"},
            timeout=JSON_TIMEOUT_SECONDS,
        )
        content = response.choices[0].message.content or ""
        return _parse_json_object(content)
    except APITimeoutError as exc:
        logger.warning("llm json timeout")
        raise AppError("LLM_TIMEOUT", "模型响应超时，请稍后重试", status=504) from exc
    except APIError as exc:
        # 与流式一样：只记类型，不记内容
        logger.error("llm json api error: %s", type(exc).__name__)
        raise AppError("LLM_ERROR", "模型返回错误，请稍后重试", status=502) from exc
    except AppError:
        raise
    except Exception as exc:
        logger.exception("llm json failed")
        raise AppError("LLM_ERROR", "模型调用失败，请稍后重试", status=502) from exc
