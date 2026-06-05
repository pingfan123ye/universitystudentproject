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

# 简繁转换（Whisper 经常输出繁体中文）
try:
    from opencc import OpenCC
    _opencc = OpenCC('t2s')  # 繁体 → 简体
except ImportError:
    _opencc = None


def _to_simplified(text: str) -> str:
    """繁体中文 → 简体中文"""
    if not text or _opencc is None:
        return text
    return _opencc.convert(text)

logger = logging.getLogger(__name__)

# 国内 HuggingFace 镜像加速下载
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

_model = None
_model_lock = threading.Lock()
_loading_started = False
_model_ready = threading.Event()  # 模型加载完成时触发（成功或失败都触发）


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
    finally:
        _model_ready.set()
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


def _transcribe_sync(pcm, vad_params: dict):
    """同步转写 —— 在独立线程中运行，避免阻塞 async 事件循环"""
    global _model
    segments, _info = _model.transcribe(
        pcm, language="zh", beam_size=5,
        vad_filter=True, vad_parameters=vad_params,
    )
    seg_list = list(segments)
    text = "".join(seg.text for seg in seg_list).strip()
    return text, len(seg_list)


async def transcribe_audio_base64(audio_b64: str) -> str:
    """将 base64 音频转写为中文文字"""
    if not audio_b64:
        return ""

    # 等待模型就绪（首次启动需 ~30s 下载/加载，之后秒级响应）
    if not _model_ready.is_set():
        logger.info("STT 等待 Whisper 模型就绪（最多 60s）...")
        if not _model_ready.wait(timeout=60):
            logger.warning("STT 等待模型超时（60s），跳过转写")
            return ""
        logger.info("STT 模型就绪，继续转写")

    model = _get_model()
    if model is None:
        logger.warning("Whisper 模型加载失败，跳过转写")
        return ""

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception:
        return ""

    if len(audio_bytes) > 44 and audio_bytes[:4] == b'RIFF':
        audio_bytes = audio_bytes[44:]

    if len(audio_bytes) < 1600:
        return ""

    # 对齐 int16（2 字节）：奇数长度会导致 "buffer size must be a multiple of element size"
    if len(audio_bytes) % 2 != 0:
        logger.warning("STT 音频字节未对齐 int16，截断 1 字节: %d -> %d", len(audio_bytes), len(audio_bytes) - 1)
        audio_bytes = audio_bytes[:-1]

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

        # 自动增益：弱信号放大后再送 VAD，避免被误判为静音
        # 用平均振幅判断整体响度（峰值可能是瞬间尖峰，不代表有效语音能量）
        gain_applied = 1.0
        if max_amp >= 0.001 and avg_amp < 0.005:
            gain_applied = min(0.8 / max(max_amp, 0.01), 50.0)
            pcm = pcm * gain_applied
            logger.info("STT 自动增益: ×%.1f (峰值 %.4f→%.4f 均值 %.6f)",
                       gain_applied, max_amp, float(np.max(np.abs(pcm))), avg_amp)

        # 音频质量门控：增益后平均振幅 + 峰值联合判断
        # 仅当峰值和均值都极低时才跳过 → 避免误杀有语音信号但均值偏低的音频
        # 跳过 Whisper 转写，避免产生幻觉输出（如"字幕by索兰娅"等）
        post_gain_avg = float(np.mean(np.abs(pcm)))
        post_gain_max = float(np.max(np.abs(pcm)))
        if post_gain_avg < 0.003 and post_gain_max < 0.1:
            logger.info("STT 音频质量门控：增益后均值 %.6f < 0.003 且峰值 %.4f < 0.1，疑似静音/噪声，跳过转写",
                       post_gain_avg, post_gain_max)
            return ""

        # 降低 VAD 阈值（默认 0.5 → 0.35），避免轻度音频被完全过滤
        # 同时提高 min_speech_duration_ms 减少碎片化检测
        vad_params = {"threshold": 0.35, "min_speech_duration_ms": 300, "min_silence_duration_ms": 400}
        # 在独立线程中运行 Whisper 推理，避免阻塞 async 事件循环
        text, seg_count = await asyncio.to_thread(_transcribe_sync, pcm, vad_params)

        # 后处理：修正 Whisper 常见误识别（音近字）
        if text:
            text = text.replace("小字", "小智")
            text = text.replace("小子", "小智")
            text = text.replace("切割", "切歌")
            # 繁转简（Whisper 经常输出繁体中文）
            text = _to_simplified(text)
        # 噪声词黑名单：过滤 Whisper 在低信噪比下的幻觉输出
        # 使用子串匹配（而非精确匹配），因为幻觉输出常带额外文字如"字幕by索兰娅"
        _NOISE_SUBSTRINGS = [
            "字幕by", "字幕由", "字幕",
            "蝙蝠绿狗", "謝謝觀賞", "感谢观看", "谢谢观看",
            "歡迎收聽", "欢迎收听",
            "索兰娅", "索蘭婭",
            "by索兰娅", "by索蘭婭",
            "蝙蝠", "绿狗",
        ]
        _text_stripped = text.strip()
        for noise_pat in _NOISE_SUBSTRINGS:
            if noise_pat in _text_stripped:
                logger.info("Whisper 噪声词已过滤 (匹配'%s'): %s", noise_pat, text)
                return ""
        # 纯英文长句（>10个ascii字符，不含中文）在中文语音助手中通常也是幻觉
        if len(_text_stripped) > 10 and all(ord(c) < 128 for c in _text_stripped if c != ' '):
            # 检查是否全是英文（没有中文字符）
            has_chinese = any('一' <= c <= '鿿' for c in _text_stripped)
            if not has_chinese:
                logger.info("Whisper 纯英文幻觉已过滤: %s", text[:60])
                return ""
        if text:
            logger.info("Whisper 转写结果 (%d chars, %d segments): %s",
                       len(text), seg_count, text[:60])
        else:
            logger.info("Whisper 转写为空（%d segments, VAD阈值=0.3，音频可能全为静音）",
                       seg_count)
        return text
    except Exception as e:
        logger.error("Whisper 转写失败: %s", e)
        return ""


async def is_available() -> bool:
    return _get_model() is not None


async def preload_model():
    """启动时调用：后台下载模型，不阻塞服务启动"""
    _start_loading()
