"""
STT 后处理纠错 —— 修正 Whisper small 模型常见的误识别。

Whisper small 在中文语音识别时经常把歌名、指令词误识别为发音相近但语义无关的词。
此模块在 STT 结果返回前端之前进行后处理纠正。
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Whisper 幻觉检测：这些模式表示音频实际是静音/噪声，模型产生了随机输出 ──
HALLUCINATION_PATTERNS: list[str] = [
    "字幕by", "字幕由", "字幕",           # 视频字幕幻觉
    "索兰娅", "索蘭婭",                   # 具体幻觉人名
    "蝙蝠绿狗",                           # 经典幻觉
    "谢谢观看", "謝謝觀賞", "感谢观看",    # 视频结尾幻觉
    "欢迎收听", "歡迎收聽",               # 播客幻觉
    "一林 ", " 一林",                     # 碎片幻觉
]

# 纯英文/乱码检测：中文语音助手中，纯英文长句 → 幻觉
_PURE_ASCII_RE = re.compile(r'^[a-zA-Z0-9\s.,!?;:\'\"\-]+$')


def _is_hallucination(text: str) -> bool:
    """检测 STT 结果是否为 Whisper 在噪声上的幻觉输出"""
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

    # 3. 文本全由单字重复组成（如"呃呃呃呃"、"好好好好"）
    if len(stripped) >= 3 and len(set(stripped)) <= 2:
        logger.info(f"STT 幻觉检测 (重复字符): {stripped[:60]}")
        return True

    return False

# 精确替换表（按长度降序，确保长匹配优先）
CORRECTIONS: dict[str, str] = {
    # ── 歌名纠错 ──
    "星天": "晴天",
    "提前": "晴天",
    "擎天": "晴天",
    "七里香": "七里香",  # 保留正确识别
    "妻离子散": "七里香",
    "清明上河图": "清明上河图",

    # ── 切歌指令纠错 ──
    "親愛的首歌": "切一首歌",
    "亲爱的首歌": "切一首歌",
    "切割": "切歌",
    "切格": "切歌",
    "且歌": "切歌",
    "欠一首歌": "切一首歌",
    "七一首歌": "切一首歌",

    # ── 唤醒词/助手名纠错 ──
    "小字": "小智",
    "小子": "小智",
    "小志": "小智",
    "小只": "小智",
    "小资": "小智",
    "小治": "小智",
    "小自": "小智",
    "消智": "小智",

    # ── 暂停指令纠错 ──
    "暂停拨号": "暂停播放",
    "暂停波": "暂停播",
    "展厅播放": "暂停播放",

    # ── 播放纠错 ──
    "播放七": "播放器",
    "波放": "播放",
    "播发": "播放",

    # ── 音量指令 ──
    "音量波": "音量播",
    "音量调大": "音量调大",
    "音量调小": "音量调小",
    "音量加大": "音量调大",
    "音量减小": "音量调小",

    # ── 设备控制纠错 ──
    "打开灯放": "打开灯",
    "关掉灯放": "关掉灯",

    # ── 播放/暂停完整指令纠错 ──
    "继序播放": "继续播放",
    "继续拨": "继续播",
    "暂停拨": "暂停播",
    "开时播放": "开始播放",
    "开时": "开始",

    # ── 常见语气词/碎片纠错 ──
    "呃呃": "",
    "嗯嗯嗯": "",
    "嗯那": "",
    "那个那个": "",
    "然后那个": "然后",
    "就是那个": "就是",
    "对不起": "",
    ",对不起": "",

    # ── 天气/时间查询 ──
    "天气肿么样": "天气怎么样",
    "天气则么样": "天气怎么样",
    "天气杂样": "天气怎么样",
    "今天天气": "今天天气",
    "明天天气": "明天天气",

    # ── 学习/考试相关 ──
    "学系": "学习",
    "学次": "学习",
    "学西": "学习",
    "放习": "复习",
    "负习": "复习",
    "背考": "备考",
    "被烤": "备考",
    "贝考": "备考",
    "备烤": "备考",
    "背靠": "备考",
    "被靠": "备考",
    "贝靠": "备考",
    "备靠": "备考",
    "背搞": "备考",
    "贝搞": "备考",
    "六集": "六级",
    "6集": "六级",
    "六极": "六级",
    "留级": "六级",
    "流利": "六级",
    "刘吉": "六级",
    "六业": "六级",
    "六页": "六级",
    "六耶": "六级",
    "六液": "六级",
    "六一": "六级",
    "真体券": "真题",
    "整体券": "真题",
    "真体全": "真题",
    "真体圈": "真题",
    "正题圈": "真题",
    "正题全": "真题",
    "真题券": "真题",
    "真题卷": "真题",

    # ── 歌名纠错 ──
    "夜空": "夜空中最亮的星",
    "辰星": "晨星",
    "水晶记": "水星记",
    "随行": "随行",
    "千与千寻": "千与千寻",
    "天空之城": "天空之城",
    "菊次郎的夏天": "菊次郎的夏天",

    # ── 音乐指令纠错 ──
    "放纵": "放首",
    "分手": "放首",
    "轻音": "轻音乐",
    "纯音": "纯音乐",
    "播放七": "播放器",
    "继序播放": "继续播放",
    "暂停拨号": "暂停播放",
    "展厅播放": "暂停播放",

    # ── 回答确认词 ──
    "好的吧": "好的",
    "行吧行吧": "行",
    "好吧好吧": "好吧",
    "可以可以": "可以",
    "对的": "对",
}

# 正则替换（更灵活的模式匹配）
REGEX_CORRECTIONS: list[tuple[str, str]] = [
    # "我想听歌了小子" → "我想听歌了小智"
    (r'了(小子|小字|小志)', r'了小智'),
    # "切换"误识别场景
    (r'^(切换|切还|切换成)', r'切歌'),
]


def correct(text: str) -> str:
    """
    对 STT 转写结果进行后处理纠错。

    Args:
        text: Whisper 转写的原始文本

    Returns:
        纠正后的文本（幻觉输出返回空字符串）
    """
    if not text or not text.strip():
        return text

    # 0. 幻觉检测：噪声上的随机输出 → 直接返回空
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
    # 纯重复字符（如"嗯嗯嗯""好好好"）
    if len(t) >= 3 and len(set(t)) <= 2:
        return True
    # 纯英文乱码
    import re as _re
    if _re.match(r'^[a-zA-Z\s.,!?;:\'\"\-]+$', t) and len(t) > 10:
        return True
    return False
