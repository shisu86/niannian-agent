"""语音合成（S8）。

用 edge-tts 在线服务把文本转成 mp3；失败抛 AppError，由接口返回统一包络。
情绪只在这里映射为语速/音调，前端不参与计算。
"""

import asyncio
import logging

import edge_tts

from app.db import connect
from app.errors import AppError

logger = logging.getLogger(__name__)

TTS_TIMEOUT_SECONDS = 15.0
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# emotion -> (rate, pitch, volume)
EMOTION_PARAMS = {
    "calm": ("+0%", "+0Hz", "+0%"),
    "low": ("-8%", "-2Hz", "+0%"),  # 低落时放慢、略低
    "anxious": ("+5%", "+2Hz", "+0%"),
    "happy": ("+8%", "+4Hz", "+0%"),
}


async def _default_voice() -> str:
    """优先用设置里的音色；读不到就退回默认。"""
    conn = await connect()
    try:
        async with conn.execute("SELECT voice_name FROM settings WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        voice = row["voice_name"] if row else None
        return voice or DEFAULT_VOICE
    except Exception:
        logger.exception("load voice_name failed, use default")
        return DEFAULT_VOICE
    finally:
        await conn.close()


def _params(emotion: str | None) -> tuple[str, str, str]:
    return EMOTION_PARAMS.get(emotion or "calm", EMOTION_PARAMS["calm"])


async def _stream_audio(text: str, voice: str, rate: str, pitch: str, volume: str) -> bytes:
    """调一次 edge-tts 并收集音频字节。"""
    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=rate,
        pitch=pitch,
        volume=volume,
    )
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buffer.extend(chunk["data"])
    return bytes(buffer)


async def synthesize(
    text: str,
    voice: str | None = None,
    emotion: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
    volume: str | None = None,
) -> bytes:
    """合成一段语音，返回 mp3 字节。

    edge-tts 是网络服务，必须设超时；否则浏览器请求会一直挂着。
    """
    voice_name = voice or await _default_voice()
    default_rate, default_pitch, default_volume = _params(emotion)
    rate = rate or default_rate
    pitch = pitch or default_pitch
    volume = volume or default_volume

    try:
        async with asyncio.timeout(TTS_TIMEOUT_SECONDS):
            try:
                audio = await _stream_audio(text, voice_name, rate, pitch, volume)
            except Exception:
                # 音色拼错时 edge-tts 可能到 stream 阶段才报错（NoAudioReceived），
                # 所以不能只在构造期兜底；非默认音色失败就用默认重试一次。
                if voice_name == DEFAULT_VOICE:
                    raise
                logger.warning("tts voice %s failed, retry default", voice_name)
                audio = await _stream_audio(text, DEFAULT_VOICE, rate, pitch, volume)
    except TimeoutError as exc:
        logger.warning("tts timeout")
        raise AppError("TTS_TIMEOUT", "语音合成超时，请稍后重试", status=504) from exc
    except AppError:
        raise
    except Exception as exc:
        # 在线服务不可用时，前端会降级到浏览器 speechSynthesis
        logger.exception("tts failed")
        raise AppError("TTS_ERROR", "语音合成失败，请稍后重试", status=502) from exc

    if not audio:
        raise AppError("TTS_ERROR", "语音合成没有返回音频", status=502)

    logger.info("tts done chars=%s voice=%s", len(text), voice_name)
    return audio
