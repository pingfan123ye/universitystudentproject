"""
STT 后处理纠错 —— 精简版（SenseVoice 准确率 ~4-6% CER）

SenseVoice-Small 的中文准确率远高于 Whisper small（~10-15% CER），
大部分纠错规则已不再需要。保留：
  1. 核心幻觉检测（SenseVoice 极少出现，但留作防护）
  2. 唤醒词/助手名纠错（音近字是 STT 固有问题）
  3. 基础质量过滤（纯英文、重复字符）
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── 幻觉检测模式（SenseVoice 极少触发，保留基本防护）──
HALLUCINATION_PATTERNS: list[str] = [
    "字幕by", "字幕由", "字幕",
    "谢谢观看", "謝謝觀賞", "感谢观看",
    "欢迎收听", "歡迎收聽",
]

# 纯英文/乱码检测
_PURE_ASCII_RE = re.compile(r'^[a-zA-Z0-9\s.,!?;:\'\"\-]+$')


def _is_hallucination(text: str) -> bool:
    """检测 STT 结果是否为噪声上的幻觉输出"""
    if not text or not text.strip():
        return False
    stripped = text.strip()

    # 1. 子串匹配已知幻觉模式
    for pat in HALLUCINATION_PATTERNS:
        if pat in stripped:
            logger.info(f"STT 幻觉检测 (匹配'{pat}'): {stripped[:60]}")
            return True

    # 2. 纯英文长句（>15 chars，无中文字符）
    if len(stripped) > 15:
        has_chinese = any('一' <= c <= '鿿' for c in stripped)
        if not has_chinese:
            logger.info(f"STT 幻觉检测 (纯英文长句): {stripped[:60]}")
            return True

    # 3. 文本全由单字重复组成（如"呃呃呃呃"）
    if len(stripped) >= 3 and len(set(stripped)) <= 2:
        logger.info(f"STT 幻觉检测 (重复字符): {stripped[:60]}")
        return True

    return False


# 核心纠错表 —— 仅保留音近字固有问题（唤醒词/高频指令）
CORRECTIONS: dict[str, str] = {
    # ── 唤醒词/助手名纠错 ──
    "小字": "小智",
    "小子": "小智",
    "小志": "小智",
    "小只": "小智",
    "小资": "小智",
    "小治": "小智",
    "小自": "小智",
    "消智": "小智",

    # ── 歌名关键纠错 ──
    "星天": "晴天",
    "提前": "晴天",
    "擎天": "晴天",

    # ── 切歌指令 ──
    "切割": "切歌",
    "切格": "切歌",
    "且歌": "切歌",

    # ── 播放指令 ──
    "波放": "播放",
    "播发": "播放",

    # ── 暂停指令 ──
    "展厅播放": "暂停播放",
    "暂停拨号": "暂停播放",

    # ── 继续播放 ──
    "继序播放": "继续播放",

    # ── 考试相关 ──
    "背考": "备考",
    "被烤": "备考",
    "备烤": "备考",
    "六集": "六级",
    "六极": "六级",

    # ── 常见语气词碎片 ──
    "呃呃": "",
    "嗯嗯嗯": "",
    "对不起": "",
    "那个那个": "",
}

# 正则替换
REGEX_CORRECTIONS: list[tuple[str, str]] = [
    # "我想听歌了小子" → "我想听歌了小智"
    (r'了(小子|小字|小志)', r'了小智'),
    # "切换"误识别
    (r'^(切换|切还|切换成)', r'切歌'),
]


def correct(text: str) -> str:
    """
    对 STT 转写结果进行后处理纠错。

    Args:
        text: STT 转写的原始文本

    Returns:
        纠正后的文本（幻觉输出返回空字符串）
    """
    if not text or not text.strip():
        return text

    # 0. 幻觉检测
    if _is_hallucination(text):
        return ""

    original = text
    result = text

    # 1. 精确替换（按长度降序，长匹配优先）
    sorted_corrections = sorted(CORRECTIONS.items(), key=lambda x: len(x[0]), reverse=True)
    for wrong, right in sorted_corrections:
        if wrong in result:
            result = result.replace(wrong, right)
            logger.debug(f"STT 纠错: '{wrong}' → '{right}'")

    # 2. 正则替换
    for pattern, replacement in REGEX_CORRECTIONS:
        new_result = re.sub(pattern, replacement, result)
        if new_result != result:
            logger.debug(f"STT 正则纠错: '{pattern}' → '{replacement}'")
            result = new_result

    # 3. 清理多余空白
    result = re.sub(r'\s+', ' ', result).strip()

    if result != original:
        logger.info(f"STT 纠错: '{original[:60]}' → '{result[:60]}'")

    return result


def is_low_quality_stt(text: str) -> bool:
    """检测低质量 STT 文本，避免被存入记忆。"""
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) < 3:
        return True
    # 纯重复字符
    if len(t) >= 3 and len(set(t)) <= 2:
        return True
    # 纯英文乱码
    if re.match(r'^[a-zA-Z\s.,!?;:\'\"\-]+$', t) and len(t) > 10:
        return True
    return False
