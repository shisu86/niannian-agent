"""语音合成接口。

POST /api/tts 返回 audio/mpeg 二进制流，是统一包络的例外；
失败时仍返回普通 JSON 错误包络（非 200）。
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.errors import get_trace_id
from app.models import TTSRequest
from app.services.tts import synthesize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tts"])


@router.post("/tts")
async def tts(payload: TTSRequest, request: Request) -> Response:
    """把文本合成 mp3；前端失败时会降级到浏览器朗读。"""
    audio = await synthesize(
        text=payload.text,
        voice=payload.voice,
        emotion=payload.emotion,
        rate=payload.rate,
        pitch=payload.pitch,
        volume=payload.volume,
    )
    logger.info("tts served trace_id=%s bytes=%s", get_trace_id(request), len(audio))
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
