"""
STT 统一调用管道 —— 本地引擎 → 讯飞 API 云端 fallback 链

本地引擎: SenseVoice-Small (推荐) 或 faster-whisper (旧版)
云端引擎: 讯飞 IAT API

环境变量 STT_LOCAL_ENGINE 控制本地引擎选择:
  - "sensevoice" (默认): 使用 SenseVoice-Small（sherpa-onnx）
  - "whisper": 使用 faster-whisper small（旧版）
  - "auto": 优先 SenseVoice，不可用时降级 Whisper
"""

import logging
import os

from config import STT_LOCAL_ENGINE

logger = logging.getLogger(__name__)

# ── 动态导入本地引擎（按配置选择）──
_local_transcribe = None
_local_is_available = None
_local_name = "none"

_engine_loaded = False


def _init_local_engine():
    """惰性初始化本地引擎（避免循环导入）"""
    global _local_transcribe, _local_is_available, _local_name, _engine_loaded
    if _engine_loaded:
        return
    _engine_loaded = True

    engine_choice = os.environ.get("STT_LOCAL_ENGINE", STT_LOCAL_ENGINE)

    # 按优先级尝试加载
    candidates = []
    if engine_choice == "sensevoice":
        candidates = ["sensevoice"]
    elif engine_choice == "whisper":
        candidates = ["whisper"]
    else:  # "auto" 或未知
        candidates = ["sensevoice", "whisper"]

    for name in candidates:
        try:
            if name == "sensevoice":
                from services.sensevoice_stt import transcribe_audio_base64 as _t
                from services.sensevoice_stt import is_available as _a
                _local_transcribe = _t
                _local_is_available = _a
                _local_name = "sensevoice"
                logger.info("STT 本地引擎: SenseVoice-Small")
                return
            elif name == "whisper":
                from services.whisper_stt import transcribe_audio_base64 as _t
                from services.whisper_stt import is_available as _a
                _local_transcribe = _t
                _local_is_available = _a
                _local_name = "whisper"
                logger.info("STT 本地引擎: faster-whisper (旧版)")
                return
        except ImportError as e:
            logger.warning("STT 引擎 %s 导入失败: %s", name, e)
            continue

    logger.warning("STT: 无可用本地引擎")


# ── 云端引擎（讯飞 IAT）──
from services.stt_service import transcribe_audio_base64 as _xunfei_transcribe
from services.stt_service import is_available as _xunfei_available

# ── 后处理纠错 ──
from services.stt_corrector import correct as _stt_correct


async def transcribe(audio_b64: str) -> str:
    """
    转写 base64 音频为文字。

    Fallback 链:
      1. 本地引擎（SenseVoice-Small 或 Whisper）
      2. 讯飞 IAT API（需要网络 + 配置）
      3. 两者都不可用 → 返回空字符串

    Returns:
        转写后的文字（已过 stt_corrector），失败返回 ""
    """
    if not audio_b64:
        return ""

    _init_local_engine()

    # ── 第一级：本地引擎 ──
    if _local_is_available is not None:
        local_ok = await _local_is_available()
        if local_ok:
            try:
                text = await _local_transcribe(audio_b64)
                if text:
                    text = _stt_correct(text)
                    if text:
                        logger.info(f"STT [{_local_name}]: {text[:60]}")
                        return text
                    else:
                        logger.info(f"STT [{_local_name}]: 转写为空或被 stt_corrector 过滤")
                else:
                    logger.info(f"STT [{_local_name}]: 返回空（可能为静音/噪声）")
            except Exception as e:
                logger.warning(f"STT [{_local_name}]: 转写异常: {e}")
        else:
            logger.info(f"STT [{_local_name}]: 模型未就绪，跳过")
    else:
        logger.info("STT: 本地引擎未配置，跳过")

    # ── 第二级：讯飞 API ──
    xunfei_ok = await _xunfei_available()
    if xunfei_ok:
        try:
            text = await _xunfei_transcribe(audio_b64)
            if text:
                text = _stt_correct(text)
                if text:
                    logger.info(f"STT [xunfei]: {text[:60]}")
                    return text
                else:
                    logger.info("STT [xunfei]: 转写为空或被 stt_corrector 过滤")
            else:
                logger.info("STT [xunfei]: 返回空")
        except Exception as e:
            logger.warning(f"STT [xunfei]: 转写异常: {e}")
    else:
        logger.info("STT [xunfei]: API 未配置或不可用，跳过")

    # ── 全部失败 ──
    logger.warning("STT: 所有引擎均不可用，返回空")
    return ""


async def get_status() -> dict:
    """
    返回 STT 管道状态，供 /health 接口和 WebSocket 推送使用。

    Returns:
        {
            "local_ready": bool,
            "local_engine": "sensevoice" | "whisper" | "none",
            "xunfei_configured": bool,
            "primary_engine": "sensevoice" | "whisper" | "xunfei" | "none",
        }
    """
    _init_local_engine()

    local_ok = False
    if _local_is_available is not None:
        local_ok = await _local_is_available()

    xunfei_ok = await _xunfei_available()

    if local_ok:
        primary = _local_name
    elif xunfei_ok:
        primary = "xunfei"
    else:
        primary = "none"

    return {
        "local_ready": local_ok,
        "local_engine": _local_name if local_ok else "none",
        "xunfei_configured": xunfei_ok,
        "primary_engine": primary,
    }
