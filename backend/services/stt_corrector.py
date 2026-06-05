"""
STT 后处理纠错 —— 修正 Whisper small 模型常见的误识别。

Whisper small 在中文语音识别时经常把歌名、指令词误识别为发音相近但语义无关的词。
此模块在 STT 结果返回前端之前进行后处理纠正。
"""

import re
import logging

logger = logging.getLogger(__name__)

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
    "那个那个": "那个",
    "然后那个": "然后",
    "就是那个": "就是",

    # ── 天气/时间查询 ──
    "天气肿么样": "天气怎么样",
    "天气则么样": "天气怎么样",
    "天气杂样": "天气怎么样",
    "今天天气": "今天天气",
    "明天天气": "明天天气",

    # ── 学习/场景指令 ──
    "学系": "学习",
    "学次": "学习",
    "学西": "学习",
    "放习": "复习",
    "负习": "复习",

    # ── 歌名纠错 ──
    "夜空": "夜空中最亮的星",
    "辰星": "晨星",
    "水晶记": "水星记",
    "随行": "随行",
    "千与千寻": "千与千寻",
    "天空之城": "天空之城",
    "菊次郎的夏天": "菊次郎的夏天",

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
        纠正后的文本
    """
    if not text or not text.strip():
        return text

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
