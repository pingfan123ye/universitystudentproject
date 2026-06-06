"""
STT 统一调用管道 —— Whisper（离线）→ 讯飞 API（云端）fallback 链

优先使用本地 faster-whisper（离线可用），失败或未就绪时降级到讯飞 API。
"""

import logging

from services.whisper_stt import transcribe_audio_base64 as _whisper_transcribe
from services.whisper_stt import is_available as _whisper_available
from services.stt_service import transcribe_audio_base64 as _xunfei_transcribe
from services.stt_service import is_available as _xunfei_available
from services.stt_corrector import correct as _stt_correct

logger = logging.getLogger(__name__)


async def transcribe(audio_b64: str) -> str:
    """
    转写 base64 音频为文字。

    Fallback 链:
      1. 本地 Whisper（faster-whisper，离线）
      2. 讯飞 IAT API（需要网络 + 配置）
      3. 两者都不可用 → 返回空字符串

    Returns:
        转写后的文字（已过 stt_corrector），失败返回 ""
    """
    if not audio_b64:
        return ""

    # ── 第一级：本地 Whisper ──
    whisper_ok = await _whisper_available()
    if whisper_ok:
        try:
            text = await _whisper_transcribe(audio_b64)
            if text:
                text = _stt_correct(text)
                if text:
                    logger.info(f"STT [whisper]: {text[:60]}")
                    return text
                else:
                    logger.info("STT [whisper]: 转写为空或被 stt_corrector 过滤")
            else:
                logger.info("STT [whisper]: 返回空（可能为静音/噪声）")
        except Exception as e:
            logger.warning(f"STT [whisper]: 转写异常: {e}")
    else:
        logger.info("STT [whisper]: 模型未就绪，跳过")

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
            "whisper_ready": bool,
            "xunfei_configured": bool,
            "primary_engine": "whisper" | "xunfei" | "none",
        }
    """
    whisper_ok = await _whisper_available()
    xunfei_ok = await _xunfei_available()

    if whisper_ok:
        primary = "whisper"
    elif xunfei_ok:
        primary = "xunfei"
    else:
        primary = "none"

    return {
        "whisper_ready": whisper_ok,
        "xunfei_configured": xunfei_ok,
        "primary_engine": primary,
    }
