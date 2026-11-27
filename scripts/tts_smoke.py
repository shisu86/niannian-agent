"""S8 语音合成冒烟（零成本，用假 edge-tts）。

验证：
1. 正常返回 mp3 字节，默认音色来自 settings；
2. 情绪映射到 rate/pitch；
3. 显式参数覆盖情绪映射；
4. 空音频、在线服务异常、超时都映射成 AppError。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/tts_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP_DIR = tempfile.mkdtemp(prefix="niannian-tts-smoke-")
os.environ["APP_DB_PATH"] = str(Path(_TMP_DIR) / "app.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from pydantic import ValidationError  # noqa: E402

import app.services.tts as tts_module  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.models import TTSRequest  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"{mark} {name}" + (f" | {detail}" if detail else ""))
    if not condition:
        failures.append(name)


class FakeCommunicate:
    """替换 edge_tts.Communicate，记录参数并返回假音频。"""

    params: dict | None = None
    error = False
    empty = False
    slow = False
    reject_voice = False

    def __init__(self, text, voice, rate, pitch, volume):
        self.voice = voice
        FakeCommunicate.params = {
            "text": text,
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
        }

    async def stream(self):
        # 模拟 edge-tts 真实行为：音色不存在时构造期不报错，stream 阶段才失败
        if FakeCommunicate.reject_voice and self.voice != tts_module.DEFAULT_VOICE:
            raise RuntimeError("NoAudioReceived")
        if FakeCommunicate.error:
            raise RuntimeError("boom")
        if FakeCommunicate.slow:
            await asyncio.sleep(0.5)
        if FakeCommunicate.empty:
            return
        yield {"type": "audio", "data": b"ID3fake"}


async def _error_code(coro) -> str | None:
    try:
        await coro
    except AppError as exc:
        return exc.code
    return None


async def main() -> None:
    await init_db()
    tts_module.edge_tts.Communicate = FakeCommunicate

    try:
        audio = await tts_module.synthesize("你好")
        check("audio returned", audio == b"ID3fake")
        check(
            "default voice from settings",
            FakeCommunicate.params is not None
            and FakeCommunicate.params["voice"] == "zh-CN-XiaoxiaoNeural",
            str(FakeCommunicate.params),
        )
        check(
            "calm params",
            FakeCommunicate.params is not None and FakeCommunicate.params["rate"] == "+0%",
        )

        await tts_module.synthesize("你好", emotion="low")
        check(
            "low rate",
            FakeCommunicate.params is not None and FakeCommunicate.params["rate"] == "-8%",
            str(FakeCommunicate.params),
        )
        check(
            "low pitch",
            FakeCommunicate.params is not None and FakeCommunicate.params["pitch"] == "-2Hz",
        )

        await tts_module.synthesize("你好", emotion="low", rate="+3%")
        check(
            "explicit rate wins",
            FakeCommunicate.params is not None and FakeCommunicate.params["rate"] == "+3%",
        )

        await tts_module.synthesize("你好", voice="en-US-JennyNeural")
        check(
            "explicit voice",
            FakeCommunicate.params is not None
            and FakeCommunicate.params["voice"] == "en-US-JennyNeural",
        )

        await tts_module.synthesize("你好", volume="-10%")
        check(
            "explicit volume",
            FakeCommunicate.params is not None and FakeCommunicate.params["volume"] == "-10%",
        )

        conn = await connect()
        try:
            await conn.execute(
                "UPDATE settings SET voice_name = ? WHERE id = 1",
                ("zh-CN-YunxiNeural",),
            )
            await conn.commit()
        finally:
            await conn.close()

        await tts_module.synthesize("你好")
        check(
            "settings voice used",
            FakeCommunicate.params is not None
            and FakeCommunicate.params["voice"] == "zh-CN-YunxiNeural",
        )

        conn = await connect()
        try:
            await conn.execute(
                "UPDATE settings SET voice_name = ? WHERE id = 1",
                (tts_module.DEFAULT_VOICE,),
            )
            await conn.commit()
        finally:
            await conn.close()

        FakeCommunicate.reject_voice = True
        audio = await tts_module.synthesize("你好", voice="xx-XX-FakeNeural")
        check(
            "invalid voice fallback",
            audio == b"ID3fake"
            and FakeCommunicate.params is not None
            and FakeCommunicate.params["voice"] == tts_module.DEFAULT_VOICE,
            str(FakeCommunicate.params),
        )
        FakeCommunicate.reject_voice = False

        try:
            TTSRequest(text="   ")
        except ValidationError:
            check("blank text rejected", True)
        else:
            check("blank text rejected", False)

        FakeCommunicate.empty = True
        check(
            "empty audio raises",
            await _error_code(tts_module.synthesize("你好")) == "TTS_ERROR",
        )
        FakeCommunicate.empty = False

        FakeCommunicate.error = True
        check(
            "service error mapped",
            await _error_code(tts_module.synthesize("你好")) == "TTS_ERROR",
        )
        FakeCommunicate.error = False

        original_timeout = tts_module.TTS_TIMEOUT_SECONDS
        tts_module.TTS_TIMEOUT_SECONDS = 0.05
        FakeCommunicate.slow = True
        check(
            "timeout mapped",
            await _error_code(tts_module.synthesize("你好")) == "TTS_TIMEOUT",
        )
        FakeCommunicate.slow = False
        tts_module.TTS_TIMEOUT_SECONDS = original_timeout
    finally:
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    if failures:
        raise SystemExit(f"FAILED: {failures}")
    print("ALL_OK")


if __name__ == "__main__":
    asyncio.run(main())
