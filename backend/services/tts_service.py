"""
Edge TTS 语音合成服务 —— 主力 TTS
将文本转为 MP3 音频流，返回 base64 编码。
失败时返回 None，由调用方降级到浏览器 speechSynthesis。
"""
import asyncio
import base64
import io
import logging

import edge_tts

logger = logging.getLogger(__name__)

# 中文发音人（按自然度降序）
ZH_VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 自然女声（推荐）
    "zh-CN-XiaoyiNeural",     # 活力女声
    "zh-CN-YunxiNeural",      # 阳光男声
    "zh-CN-YunjianNeural",    # 新闻男声
    "zh-CN-YunxiaNeural",     # 邻家男声
]

TTS_TIMEOUT = 15  # 单次 TTS 超时（秒）


async def text_to_speech_base64(text: str, voice: str | None = None) -> str | None:
    """
    将文本转为 MP3 音频，返回 base64 编码字符串。
    失败或超时时返回 None。
    """
    text = text.strip()
    if not text:
        return None

    _voice = voice or ZH_VOICES[0]

    try:
        communicate = edge_tts.Communicate(text, _voice)
        audio_bytes = io.BytesIO()

        stream = communicate.stream()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    anext(stream),
                    timeout=TTS_TIMEOUT,
                )
            except StopAsyncIteration:
                break
            if chunk["type"] == "audio":
                audio_bytes.write(chunk["data"])

        audio_data = audio_bytes.getvalue()
        if not audio_data:
            logger.warning("Edge TTS 返回空音频")
            return None

        return base64.b64encode(audio_data).decode("utf-8")

    except asyncio.TimeoutError:
        logger.warning("Edge TTS 超时 (%s)", text[:30])
        return None
    except Exception as e:
        logger.warning("Edge TTS 失败: %s — %s", type(e).__name__, str(e)[:60])
        return None


async def is_available() -> bool:
    """检查 Edge TTS 服务是否可达"""
    try:
        await asyncio.wait_for(edge_tts.list_voices(), timeout=5.0)
        return True
    except Exception:
        return False
