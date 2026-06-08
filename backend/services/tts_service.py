"""
TTS 语音合成服务
fallback 链（由 TTS_FALLBACK_CHAIN 环境变量控制）:
  1. Edge TTS（微软 Xiaoxiao 神经语音，云端高质量主力）
  2. VITS MeloTTS（sherpa-onnx 离线备选，中英双语）
  3. pyttsx3（Windows SAPI 本地引擎，最后兜底）
"""
import asyncio
import base64
import io
import logging
import os
import re as _re
import threading
import time

import edge_tts

logger = logging.getLogger(__name__)

from config import TTS_FALLBACK_CHAIN

# 解析 fallback 链顺序
_FALLBACK_ENGINES = [e.strip() for e in TTS_FALLBACK_CHAIN.split(",") if e.strip()]


# ═══════════════════════════════════════
# 引擎 2: VITS MeloTTS (sherpa-onnx 离线备选)
# ═══════════════════════════════════════

_kokoro_loaded = False

def _ensure_kokoro_imported():
    global _kokoro_loaded
    if _kokoro_loaded:
        return True
    try:
        from services.qwen3_tts import text_to_speech_base64 as _kokoro_tts
        from services.qwen3_tts import is_available as _kokoro_available
        _kokoro_loaded = True
        return True
    except ImportError:
        return False


async def _kokoro_base64(text: str) -> str | None:
    if not _ensure_kokoro_imported():
        return None
    from services.qwen3_tts import text_to_speech_base64
    from services.qwen3_tts import is_available
    if not await is_available():
        return None
    return await text_to_speech_base64(text)


# ═══════════════════════════════════════
# 引擎 1: Edge TTS (微软云端，最高优先级)
# ═══════════════════════════════════════

ZH_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
]


async def _edge_tts_base64(text: str, voice: str | None = None) -> str | None:
    """Edge TTS 转 base64，直接合成 + 10s 超时 + 一次重试"""
    _voice = voice or ZH_VOICES[0]
    for attempt in range(2):
        try:
            communicate = edge_tts.Communicate(text, _voice)

            async def _collect() -> bytes:
                buf = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])
                return buf.getvalue()

            audio_data = await asyncio.wait_for(_collect(), timeout=10.0)
            if audio_data:
                logger.info("Edge TTS 合成成功 (%d bytes, 尝试 %d/2)", len(audio_data), attempt + 1)
                return base64.b64encode(audio_data).decode("utf-8")
        except asyncio.TimeoutError:
            logger.warning("Edge TTS 超时 (尝试 %d/2)", attempt + 1)
            break  # 一次超时就说明网络不通，不用再试
        except Exception as e:
            logger.warning("Edge TTS 异常 (尝试 %d/2): %s", attempt + 1, str(e)[:60])
    return None


# ═══════════════════════════════════════
# 引擎 3: pyttsx3 (Windows SAPI，最后兜底)
# ═══════════════════════════════════════

_pyttsx3_engine = None
_pyttsx3_lock = threading.Lock()


def _get_pyttsx3_engine():
    global _pyttsx3_engine
    if _pyttsx3_engine is not None:
        return _pyttsx3_engine
    with _pyttsx3_lock:
        if _pyttsx3_engine is not None:
            return _pyttsx3_engine
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty('voices')
            for v in voices:
                if 'zh-CN' in str(v.languages) or 'HUIHUI' in v.id:
                    engine.setProperty('voice', v.id)
                    break
            engine.setProperty('rate', 130)
            engine.setProperty('volume', 1.0)
            _pyttsx3_engine = engine
            logger.info("pyttsx3 引擎已就绪: %s", engine.getProperty('voice'))
        except Exception as e:
            logger.warning("pyttsx3 初始化失败: %s", e)
            _pyttsx3_engine = None
    return _pyttsx3_engine


def _speak_sync(text: str):
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
    engine = _get_pyttsx3_engine()
    return engine is not None


# ═══════════════════════════════════════
# 主入口: 按配置的 fallback 链合成语音
# ═══════════════════════════════════════

async def text_to_speech_base64(text: str, voice: str | None = None) -> str | None:
    """
    主入口：按 TTS_FALLBACK_CHAIN 顺序尝试合成语音。

    Returns:
        base64 编码的音频数据（WAV/mp3），或 None（pyttsx3 直接发音时）
    """
    text = text.strip()
    if not text:
        return None

    for engine_name in _FALLBACK_ENGINES:
        try:
            if engine_name == "qwen3_tts":
                audio_b64 = await _kokoro_base64(text)
                if audio_b64:
                    return audio_b64

            elif engine_name == "edge_tts":
                audio_b64 = await _edge_tts_base64(text, voice)
                if audio_b64:
                    return audio_b64

            elif engine_name == "pyttsx3":
                spoken = await pyttsx3_speak(text)
                if spoken:
                    return None  # 已通过扬声器发音

        except Exception as e:
            logger.warning("TTS 引擎 %s 失败: %s", engine_name, e)
            continue

    logger.warning("所有 TTS 引擎均失败")
    return None


async def is_available() -> bool:
    """检查是否有任一 TTS 引擎可用"""
    for engine_name in _FALLBACK_ENGINES:
        try:
            if engine_name == "qwen3_tts":
                if _ensure_kokoro_imported():
                    from services.qwen3_tts import is_available as _a
                    if await _a():
                        return True
            elif engine_name == "edge_tts":
                # 不调 list_voices()（可能被墙），直接尝试即可
                # Edge TTS 在合成时自带 10s 超时，这里只检查网络可达性
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection("speech.platform.bing.com", 443),
                        timeout=3.0,
                    )
                    writer.close()
                    return True
                except Exception:
                    pass
            elif engine_name == "pyttsx3":
                if await is_pyttsx3_available():
                    return True
        except Exception:
            pass
    return False


# ═══════════════════════════════════════
# TTS 文本清洗
# ═══════════════════════════════════════

def clean_for_tts(text: str) -> str:
    """清洗 Markdown / 结构化格式字符 + Emoji，让 TTS 只朗读纯文本"""
    # 0. 最终防线：清除所有 [ACTIONS] 标签
    #    内联正则避免依赖 llm_service 的 import（llm_service 依赖 ollama 等重型包）
    _ACTIONS_STRIP_RE = _re.compile(
        r'[\[【]ACTIONS[]】].*?[\[【]/ACTIONS[]】]', _re.DOTALL
    )
    text = _ACTIONS_STRIP_RE.sub('', text).strip()
    if not text:
        return ""

    # 0.5 去除 Emoji 表情符号
    _EMOJI_BLOCKS = _re.compile(
        '['
        '\U0001F600-\U0001F64F'
        '\U0001F300-\U0001F5FF'
        '\U0001F680-\U0001F6FF'
        '\U0001F1E0-\U0001F1FF'
        '\U00002702-\U000027B0'
        '\U0001F900-\U0001F9FF'
        '\U0001FA00-\U0001FA6F'
        '\U0001FA70-\U0001FAFF'
        '\U00002600-\U000026FF'
        '\U0000FE00-\U0000FE0F'
        '\U0000200D'
        ']+',
        flags=_re.UNICODE,
    )
    _EXTRA_SYMBOLS = _re.compile(
        '['
        '©®™ℹ'
        '⏏⏩-⏳⏸-⏺'
        'Ⓜ'
        '▪▫▶◀◻-◾'
        '㊗㊙〰〽'
        '⭐⭕'
        ']+',
        flags=_re.UNICODE,
    )
    text = _EMOJI_BLOCKS.sub('', text)
    text = _EXTRA_SYMBOLS.sub('', text)
    if not text.strip():
        return ""

    # 1. 去掉代码块
    text = _re.sub(r'```[\s\S]*?```', '', text)
    # 2. 去掉行内代码
    text = _re.sub(r'`([^`]+)`', r'\1', text)
    # 3. 去掉粗体标记
    text = _re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = _re.sub(r'__([^_]+)__', r'\1', text)
    # 4. 去掉斜体标记
    text = _re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)
    text = _re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'\1', text)
    # 5. 去掉标题标记
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
    # 6. 去掉无序列表标记
    text = _re.sub(r'^[\-\*\+]\s+', '', text, flags=_re.MULTILINE)
    # 7. 去掉有序列表标记
    text = _re.sub(r'^\d+\.\s+', '', text, flags=_re.MULTILINE)
    # 8. 去掉删除线
    text = _re.sub(r'~~([^~]+)~~', r'\1', text)
    # 9. 去掉多余空白行
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
