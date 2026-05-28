"""
本地 Whisper STT 服务 —— faster-whisper (CPU)
启动时后台下载 small 模型（~1GB，仅首次），就绪后秒级转写。
"""
import asyncio
import base64
import logging
import os
import threading

import numpy as np

logger = logging.getLogger(__name__)

# 国内 HuggingFace 镜像加速下载
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

_model = None
_model_lock = threading.Lock()
_loading_started = False


def _load_model_sync():
    """同步加载模型（在后台线程运行）"""
    global _model
    try:
        logger.info("正在下载 faster-whisper small 模型（~1GB，仅首次，请耐心等待）...")
        from faster_whisper import WhisperModel
        _model = WhisperModel("small", device="cpu", compute_type="int8")
        logger.info("faster-whisper 模型已就绪")
    except Exception as e:
        logger.warning("faster-whisper 加载失败: %s", e)
        _model = None
    return _model


def _start_loading():
    """启动后台加载（非阻塞）"""
    global _loading_started
    if _loading_started:
        return
    _loading_started = True
    t = threading.Thread(target=_load_model_sync, daemon=True)
    t.start()
    logger.info("后台线程已启动：正在加载 Whisper 模型...")


def _get_model():
    """获取模型（不阻塞，未就绪返回 None）"""
    return _model


async def transcribe_audio_base64(audio_b64: str) -> str:
    """将 base64 音频转写为中文文字"""
    if not audio_b64:
        return ""

    model = _get_model()
    if model is None:
        logger.warning("Whisper 模型未就绪（正在下载或加载失败），跳过转写")
        return ""

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception:
        return ""

    if len(audio_bytes) > 44 and audio_bytes[:4] == b'RIFF':
        audio_bytes = audio_bytes[44:]

    if len(audio_bytes) < 1600:
        return ""

    try:
        pcm = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        # 诊断：检查 PCM 振幅
        max_amp = float(np.max(np.abs(pcm)))
        avg_amp = float(np.mean(np.abs(pcm)))
        logger.info("STT 音频诊断: 采样数=%d 最大振幅=%.4f 平均振幅=%.6f",
                    len(pcm), max_amp, avg_amp)
        if max_amp < 0.001:
            logger.warning("STT 音频诊断: 振幅极低，可能是静音或麦克风被占用")

        # 降低 VAD 阈值（默认 0.5 → 0.3），避免轻度音频被完全过滤
        vad_params = {"threshold": 0.3, "min_speech_duration_ms": 250}
        segments, _info = model.transcribe(
            pcm, language="zh", beam_size=5,
            vad_filter=True, vad_parameters=vad_params,
        )
        seg_list = list(segments)
        text = "".join(seg.text for seg in seg_list).strip()

        if text:
            logger.info("Whisper 转写结果 (%d chars, %d segments): %s",
                       len(text), len(seg_list), text[:60])
        else:
            logger.info("Whisper 转写为空（%d segments, VAD阈值=0.3，音频可能全为静音）",
                       len(seg_list))
        return text
    except Exception as e:
        logger.error("Whisper 转写失败: %s", e)
        return ""


async def is_available() -> bool:
    return _get_model() is not None


async def preload_model():
    """启动时调用：后台下载模型，不阻塞服务启动"""
    _start_loading()
