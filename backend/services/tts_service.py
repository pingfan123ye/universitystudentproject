"""
TTS 语音合成服务
优先级：
  1. pyttsx3（Windows 本地 SAPI，零延迟直接发声）
  2. Edge TTS（云端，网络不可达时超时降级）
"""
import asyncio
import base64
import io
import logging
import threading

import edge_tts

logger = logging.getLogger(__name__)

# ── pyttsx3 本地引擎 ──
_pyttsx3_engine = None
_pyttsx3_lock = threading.Lock()


def _get_pyttsx3_engine():
    """线程安全地获取/初始化 pyttsx3 引擎"""
    global _pyttsx3_engine
    if _pyttsx3_engine is not None:
        return _pyttsx3_engine
    with _pyttsx3_lock:
        if _pyttsx3_engine is not None:
            return _pyttsx3_engine
        try:
            import pyttsx3
            engine = pyttsx3.init()
            # 尝试设置中文语音
            voices = engine.getProperty('voices')
            for v in voices:
                if 'zh-CN' in str(v.languages) or 'HUIHUI' in v.id:
                    engine.setProperty('voice', v.id)
                    break
            engine.setProperty('rate', 130)   # 语速（调慢更自然）
            engine.setProperty('volume', 1.0) # 音量
            _pyttsx3_engine = engine
            logger.info("pyttsx3 引擎已就绪: %s", engine.getProperty('voice'))
        except Exception as e:
            logger.warning("pyttsx3 初始化失败: %s", e)
            _pyttsx3_engine = None
    return _pyttsx3_engine


def _speak_sync(text: str):
    """同步阻塞调用 pyttsx3 发音（在后台线程执行）"""
    engine = _get_pyttsx3_engine()
    if engine is None:
        raise RuntimeError("pyttsx3 不可用")
    engine.say(text)
    engine.runAndWait()


async def pyttsx3_speak(text: str) -> bool:
    """用 pyttsx3 直接发音（通过 Windows SAPI 本地扬声器）"""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _speak_sync, text)
        return True
    except Exception as e:
        logger.warning("pyttsx3 发音失败: %s", e)
        return False


async def is_pyttsx3_available() -> bool:
    """检查 pyttsx3 是否可用"""
    engine = _get_pyttsx3_engine()
    return engine is not None


# ── Edge TTS 后备引擎 ──

# 中文发音人（按自然度降序）
ZH_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
]


async def _edge_tts_base64(text: str, voice: str | None = None) -> str | None:
    """Edge TTS 转 base64，带整体超时和重试"""
    _voice = voice or ZH_VOICES[0]
    for attempt in range(2):
        try:
            communicate = edge_tts.Communicate(text, _voice)
            audio_bytes = io.BytesIO()
            async with asyncio.timeout(5.0):
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_bytes.write(chunk["data"])
            audio_data = audio_bytes.getvalue()
            if audio_data:
                logger.info("Edge TTS 合成成功 (%d bytes, 尝试 %d/2)", len(audio_data), attempt + 1)
                return base64.b64encode(audio_data).decode("utf-8")
        except asyncio.TimeoutError:
            logger.warning("Edge TTS 整体超时 (尝试 %d/2, text=%s)", attempt + 1, text[:30])
        except Exception as e:
            logger.warning("Edge TTS 异常 (尝试 %d/2): %s — %s", attempt + 1, type(e).__name__, str(e)[:60])
    return None


async def text_to_speech_base64(text: str, voice: str | None = None) -> str | None:
    """
    主入口：优先 Edge TTS（自然语音），pyttsx3 本地后备（离线时用）。
    """
    text = text.strip()
    if not text:
        return None

    # 1. 优先 Edge TTS 云端引擎（免费，音质自然）
    audio_b64 = await _edge_tts_base64(text, voice)
    if audio_b64:
        return audio_b64

    # 2. pyttsx3 本地引擎（离线后备，直接扬声器发音）
    spoken = await pyttsx3_speak(text)
    if spoken:
        return None  # 已通过扬声器发音，不需要返回音频

    # 3. ChatTTS 本地自然语音（音质最佳但加载慢，最后后备）
    try:
        audio_b64 = await _chattts_base64(text)
        if audio_b64:
            return audio_b64
    except Exception as e:
        logger.warning("ChatTTS 失败: %s", e)

    return None


# ── ChatTTS 自然语音引擎 ──
_chattts_instance = None
_chattts_lock = threading.Lock()


def _get_chattts():
    global _chattts_instance
    if _chattts_instance is not None:
        return _chattts_instance
    with _chattts_lock:
        if _chattts_instance is not None:
            return _chattts_instance
        try:
            from ChatTTS import Chat
            _chattts_instance = Chat()
            _chattts_instance.load(compile=False, device="cpu", source="huggingface")
            logger.info("ChatTTS 引擎已就绪 (CPU)")
        except Exception as e:
            logger.warning("ChatTTS 初始化失败: %s", e)
            _chattts_instance = None
    return _chattts_instance


async def _chattts_base64(text: str) -> str | None:
    """
    使用 ChatTTS 合成语音，返回 base64 编码的 WAV 音频。
    如果 ChatTTS 不可用返回 None。
    """
    chat = _get_chattts()
    if chat is None:
        return None

    import scipy.io.wavfile
    loop = asyncio.get_event_loop()

    def _sync_infer():
        wavs = chat.infer([text])
        wav = wavs[0]
        buf = io.BytesIO()
        scipy.io.wavfile.write(buf, 24000, wav.astype("float32"))
        return base64.b64encode(buf.getvalue()).decode()

    return await loop.run_in_executor(None, _sync_infer)


async def is_available() -> bool:
    """检查是否有任一 TTS 引擎可用"""
    return await is_pyttsx3_available() or await _edge_tts_check()


async def _edge_tts_check() -> bool:
    try:
        await asyncio.wait_for(edge_tts.list_voices(), timeout=5.0)
        return True
    except Exception:
        return False
