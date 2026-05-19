"""
意图路由分发器 —— 语义完整性优先

原则:
1. 若整句核心操作小爱能完成 → 直接小爱
2. 若整句含小爱不能完成的 → 大模型（大模型可反向调用小爱）
3. 禁止"伪调用": 大模型说"调用小爱"必须触发真实执行
"""
import re
from dataclasses import dataclass, field


@dataclass
class RouteDecision:
    path: str
    confidence: float
    reason: str
    matched_key: str = ""
    device_actions: list = field(default_factory=list)
    music_action: dict | None = None


# ═══════════════════════════════════════
# Reasonix 检测（工作助手 / 克劳德 / 贾维斯）
# ═══════════════════════════════════════
REASONIX_START = [
    (re.compile(r'^(工作助手|编程助手)'), '工作助手'),
    (re.compile(r'^(克劳德|claude|Claude|CLAUDE)'), '克劳德'),
    (re.compile(r'^(贾维斯|jarvis|Jarvis|JARVIS)'), '贾维斯'),
    (re.compile(r'^(帮我写代码|帮我写个|帮我写一段|帮我写一个)'), '编程请求'),
    (re.compile(r'^写一个.*(?:脚本|程序|代码|函数|功能|工具)'), '编程请求'),
    (re.compile(r'^(运行程序|执行任务|执行脚本|跑一下)'), '执行请求'),
    (re.compile(r'^(帮我调试|修复.*bug|优化.*代码|重构)'), '编程请求'),
]
REASONIX_MID = [
    re.compile(r'(?:工作助手|编程助手).*(?:帮|写|代码|脚本|执行|运行|命令|编程|优化|修复|生成|创建)'),
    re.compile(r'(?:克劳德|claude|Claude).*(?:帮|写|代码|脚本|执行|运行|命令|编程|优化|修复|生成|创建)'),
    re.compile(r'(?:贾维斯|jarvis|Jarvis).*(?:帮|写|代码|脚本|执行|运行|命令|编程|优化|修复|生成|创建)'),
    re.compile(r'(?:帮|让|用|叫|请).*(?:工作助手|克劳德|贾维斯)'),
]


def _check_reasonix(text: str) -> RouteDecision | None:
    for p, label in REASONIX_START:
        if p.search(text):
            return RouteDecision(path="reasonix", confidence=0.95, reason=f"Reasonix: {label}", matched_key=label)
    for p in REASONIX_MID:
        if p.search(text):
            return RouteDecision(path="reasonix", confidence=0.8, reason="Reasonix句中提及", matched_key="句中提及")
    return None


# ═══════════════════════════════════════
# 小爱能力定义
# ═══════════════════════════════════════

# --- 设备控制 ---
DEVICE_ACTIONS_BY_TYPE = {
    "on":  ["打开", "开", "开了", "启动"],
    "off": ["关闭", "关掉", "关", "关了", "停止"],
    "up":  ["调高", "调大"],
    "down":["调低", "调小"],
    "set": ["调到", "调成", "设置温度"],
}

DEVICE_NAMES_SORTED = [
    "卧室灯", "客厅灯", "厨房灯", "卫生间灯", "书房灯",
    "客厅窗帘", "灯", "灯光", "窗帘", "空调", "热水器", "风扇", "电视",
]

DEVICE_MAP = {
    "卧室灯": "bedroom_light", "客厅灯": "living_light",
    "厨房灯": "kitchen_light", "卫生间灯": "bathroom_light",
    "书房灯": "study_light", "灯": "living_light", "灯光": "living_light",
    "窗帘": "living_curtain", "客厅窗帘": "living_curtain",
    "空调": "ac", "热水器": "water_heater",
    "风扇": "fan", "电视": "tv",
}

# --- 音乐控制 ---
MUSIC_ACTIONS = {
    "play":  ["播放音乐", "播放歌曲", "放歌", "放音乐", "放点音乐", "放个音乐",
              "来首歌", "唱歌", "唱首歌", "听歌", "放一首", "播一首", "播音乐", "播歌",
              "放背景音乐", "播放背景音乐", "播放轻音乐", "来点音乐", "来首",
              "放轻松音乐", "放点歌", "放首歌", "来点歌", "播点音乐", "播点歌",
              "来段音乐", "想听音乐", "想听歌", "放个歌", "给我放", "给我唱",
              "来点背景音乐", "放些音乐", "放些歌", "播一下音乐", "播一下歌"],
    "pause": ["暂停音乐", "暂停播放", "暂停", "停止播放", "停止音乐",
              "关掉音乐", "关闭音乐", "关音乐", "停掉音乐", "别放了", "别唱了",
              "停了音乐", "把音乐关了", "把音乐关掉", "关背景音乐", "别播了", "别放了"],
    "next":  ["下一首", "下一曲", "换一首", "切歌", "换首歌", "换个歌"],
    "prev":  ["上一首", "上一曲"],
}

VOLUME_KEYWORDS = ["音量", "大声", "小声", "静音", "声音"]


def _detect_music_action(text: str) -> dict | None:
    """从文本中检测音乐控制意图"""
    # 关闭/暂停类（优先检测，因为"关音乐"应该被识别）
    for kw in MUSIC_ACTIONS["pause"]:
        if kw in text:
            return {"action": "pause"}
    # 播放类
    for kw in MUSIC_ACTIONS["play"]:
        if kw in text:
            return {"action": "play"}
    # 切歌类
    for kw in MUSIC_ACTIONS["next"]:
        if kw in text:
            return {"action": "next"}
    for kw in MUSIC_ACTIONS["prev"]:
        if kw in text:
            return {"action": "prev"}
    # 音量控制
    if any(kw in text for kw in VOLUME_KEYWORDS):
        return {"action": "play"}  # 音量指令附带播放能力
    return None


def _detect_device_actions(text: str) -> list[dict]:
    """从文本中提取所有设备操作"""
    actions = []
    for device_kw in DEVICE_NAMES_SORTED:
        pos = text.find(device_kw)
        if pos == -1:
            continue
        device_id = DEVICE_MAP.get(device_kw)
        if not device_id:
            continue
        if device_id in [a["device"] for a in actions]:
            continue

        # 找最近的动词
        best_action_type = "toggle"
        best_dist = 999
        for atype, kws in DEVICE_ACTIONS_BY_TYPE.items():
            for kw in kws:
                apos = text.find(kw)
                if apos != -1 and abs(apos - pos) < best_dist:
                    best_dist = abs(apos - pos)
                    best_action_type = atype

        if best_dist < 20:  # 动词在设备20字以内
            actions.append({"device": device_id, "action": best_action_type})
    return actions


# --- 场景 ---
SCENE_KEYWORDS = ["离家模式", "回家模式", "晚安", "起床模式", "观影模式", "阅读模式"]

# --- 查询（小爱不能真正处理，但先尝试） ---
QUERY_KEYWORDS = ["天气", "温度", "湿度", "空气质量", "时间", "几点", "日期", "星期几", "今天几号"]

# --- 工具（部分小爱能处理，先尝试） ---
UTILITY_KEYWORDS = ["闹钟", "提醒", "倒计时", "计时"]
# 音乐关键词已在 MUSIC_ACTIONS 中处理，不在此重复


QUESTION_PATTERNS = [
    re.compile(r'怎么(?!样|么|办)|如何(?!何)|怎样(?!的|样)'),  # "怎么"+动词 (不是"怎么样")
    re.compile(r'能不能|可不可以|知不知道|你知道.*怎么'),
    re.compile(r'教我|告诉我|请问'),
]


def _is_question(text: str) -> bool:
    """检测是否为询问句（询问方法/知识，而非执行操作）"""
    for p in QUESTION_PATTERNS:
        if p.search(text):
            return True
    return False


def _check_xiaoai(text: str) -> RouteDecision | None:
    """语义检测：整句核心操作是否小爱可完成"""
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)

    # 纯语义判断: 有没有小爱能做的事
    has_actionable = bool(device) or bool(music) or is_scene

    if has_actionable:
        parts = []
        if device:
            parts.append(f"设备:{len(device)}项")
        if music:
            parts.append(f"音乐:{music['action']}")
        if is_scene:
            parts.append("场景")

        return RouteDecision(
            path="xiaoai",
            confidence=0.9,
            reason=" | ".join(parts),
            matched_key=" | ".join(parts),
            device_actions=device,
            music_action=music,
        )

    # 查询 / 工具 — 小爱可能处理，先尝试
    for kw in QUERY_KEYWORDS:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"查询: {kw}", matched_key="查询")
    for kw in UTILITY_KEYWORDS:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"工具: {kw}", matched_key="工具")

    return None


def classify(text: str) -> RouteDecision:
    """语义完整性优先路由"""
    text = text.strip()

    # 1. Reasonix（工作助手 / 克劳德 / 贾维斯 / 编程请求）
    d = _check_reasonix(text)
    if d:
        return d

    # 2. 小爱（语义完整性判断 — 但询问句排除）
    if not _is_question(text):
        d = _check_xiaoai(text)
        if d:
            return d

    # 3. 大模型兜底
    return RouteDecision(path="llm", confidence=0.5, reason="大模型兜底", matched_key="默认")
