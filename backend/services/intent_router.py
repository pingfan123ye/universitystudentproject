"""
意图路由分发器 —— 五类路由 + 混合意图拆分 (v0.3.0 精简版)

SenseVoice-Small 中文准确率 ~4-6% CER（vs Whisper ~10-15%），
大量 STT 误识别补偿词条已不再需要。

路由路径:
  reasonix  → 编程/工作助手
  xiaoai    → 设备控制/音乐/场景
  info_query → 信息查询（天气/新闻/百科等，进双引擎调度）
  mixed     → 混合意图（拆分子任务分别路由）
  llm       → 大模型兜底（含强制走大模型）
  noise     → 音乐串扰过滤（忽略）
"""
import re
from dataclasses import dataclass, field


@dataclass
class RouteDecision:
    path: str               # reasonix | cet6 | xiaoai | info_query | mixed | llm
    confidence: float
    reason: str
    matched_key: str = ""
    device_actions: list = field(default_factory=list)
    music_action: dict | None = None
    sub_tasks: list = field(default_factory=list)  # mixed: 拆分子任务
    force_llm: bool = False  # 用户明确要求走大模型


# ═══════════════════════════════════════
# 强制走大模型前缀
# ═══════════════════════════════════════
FORCE_LLM_PREFIX = [
    re.compile(r'^(让AI|用大模型|让大模型|用AI|叫AI|叫大模型)'),
    re.compile(r'^(你(来|帮我))'),
]


def _check_force_llm(text: str) -> bool:
    for p in FORCE_LLM_PREFIX:
        if p.search(text):
            return True
    if text.startswith("你") and ("觉得" in text or "认为" in text or "建议" in text or "推荐" in text):
        return True
    return False


# ═══════════════════════════════════════
# Reasonix 检测
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
# 设备控制能力定义
# ═══════════════════════════════════════

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

# ── 音乐动作关键词（精简版，SenseVoice 准确率提升后不再需要大量冗余词条）──
MUSIC_ACTIONS = {
    "play":  ["播放音乐", "播放歌曲", "放歌", "放音乐", "放点音乐", "放个音乐",
              "来首歌", "唱歌", "听歌", "放一首", "播一首", "播音乐", "播歌",
              "来点音乐", "来首", "放点歌", "放首歌", "来点歌", "播点音乐", "播点歌",
              "想听音乐", "想听歌", "给我放", "给我唱", "放些音乐", "放些歌",
              "播放", "我想听", "想听", "我要听", "点歌", "点一首", "来一首",
              "随便来一首", "随便来一个", "随便放", "随机播放", "随意来一首",
              "放点", "放些", "来点", "播点", "放个", "来首"],
    "pause": ["暂停音乐", "暂停播放", "暂停", "停止播放", "停止音乐",
              "关掉音乐", "关闭音乐", "关音乐", "停掉音乐", "别放了", "别唱了",
              "别播了", "别放了"],
    "next":  ["下一首", "下一首歌", "下一曲", "换一首", "切歌", "换首歌", "换个歌", "切一首"],
    "prev":  ["上一首", "上一曲"],
    "resume": ["继续播放", "继续", "恢复播放"],
    "stop": ["停了", "停止", "关掉音乐", "关闭音乐"],
}

VOLUME_KEYWORDS = ["音量", "大声", "小声", "静音", "声音"]

SCENE_KEYWORDS = ["离家模式", "回家模式", "晚安", "起床模式", "观影模式", "阅读模式"]

# ── 严格音乐播放意图正则（精简版）──
MUSIC_PLAY_PATTERNS = [
    re.compile(r"(播放|放|来一首|我想听|我要听|点一首|给我放|给你放)\s*[一-龥a-zA-Z0-9]+"),
    re.compile(r"听\s*[一-龥a-zA-Z0-9]+\s*(这首歌|这首)"),
    re.compile(r"播\s*[一-龥a-zA-Z0-9]+"),
    # 泛化音乐请求
    re.compile(r"(想听|听|来点|放点|播点|来首)\s*(歌|音乐|歌曲|什么歌|啥歌)"),
    # 间隔式音乐请求："放点不吵的学习歌"
    re.compile(r"(放点|放些|来点|来些|播点|播些|想听点).{0,15}(歌|音乐|歌曲|听的)"),
    # 容器词 + 音乐关键词
    re.compile(r"(顺便|再|也|还|帮我|给我|帮忙)\s*(放点|放些|来点|播点).{0,15}(歌|音乐|歌曲)"),
    re.compile(r"(有点|好)\s*(想听)\s*(歌|音乐|歌曲)"),
    # 随机/泛化请求
    re.compile(r"(随便|随意|随机|任意).*(歌|音乐|歌曲|首|曲|什么|啥)"),
    re.compile(r"(什么|啥).*(都可以|都行|也行|都可)"),
]


def _is_music_play_intent(text: str) -> bool:
    for p in MUSIC_PLAY_PATTERNS:
        if p.search(text):
            return True
    return False


XIAOAI_QUERY = ["时间", "几点"]
XIAOAI_UTILITY = ["闹钟", "提醒", "倒计时", "计时"]


def _extract_music_query(text: str) -> str:
    from services.music_service import clean_music_query
    return clean_music_query(text)


def _detect_music_action_fallback(text: str) -> dict | None:
    """宽松匹配：严格正则失败但明显是音乐请求时的补偿"""
    play_hints = ["播放", "放首", "放一", "想听", "来一首", "来一",
                  "放点", "放些", "来点", "播点", "播", "听"]
    music_hints = ["歌", "音乐", "曲", "背景", "轻音", "纯音"]
    has_play = any(p in text for p in play_hints)
    has_music = any(m in text for m in music_hints)
    if has_play and has_music:
        query = _extract_music_query(text)
        result = {"action": "play"}
        if query:
            result["query"] = query
        return result
    return None


def _detect_music_action(text: str) -> dict | None:
    action = None
    for kw in MUSIC_ACTIONS["resume"]:
        if kw in text:
            action = "resume"
            break
    if not action:
        for kw in MUSIC_ACTIONS["pause"]:
            if kw in text:
                action = "pause"
                break
    if not action:
        for kw in MUSIC_ACTIONS["stop"]:
            if kw in text:
                action = "stop"
                break
    if not action:
        for kw in MUSIC_ACTIONS["play"]:
            if kw in text:
                if _is_music_play_intent(text):
                    action = "play"
                break
    if not action:
        for kw in MUSIC_ACTIONS["next"]:
            if kw in text:
                action = "next"
                break
    if not action:
        for kw in MUSIC_ACTIONS["prev"]:
            if kw in text:
                action = "prev"
                break

    if not action and any(kw in text for kw in VOLUME_KEYWORDS):
        if _is_music_play_intent(text):
            action = "play"

    # 宽松 fallback
    if not action:
        fallback = _detect_music_action_fallback(text)
        if fallback:
            action = fallback["action"]

    if action:
        result = {"action": action}
        if action == "play":
            query = _extract_music_query(text)
            if query:
                result["query"] = query
        return result
    return None


def _detect_device_actions(text: str) -> list[dict]:
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
        best_action_type = "toggle"
        best_dist = 999
        for atype, kws in DEVICE_ACTIONS_BY_TYPE.items():
            for kw in kws:
                apos = text.find(kw)
                if apos != -1 and abs(apos - pos) < best_dist:
                    best_dist = abs(apos - pos)
                    best_action_type = atype
        if best_dist < 20:
            actions.append({"device": device_id, "action": best_action_type})
    return actions


# ═══════════════════════════════════════
# 信息查询检测
# ═══════════════════════════════════════

INFO_QUERY_KEYWORDS = [
    "天气", "新闻", "头条", "热搜", "股票", "汇率", "油价",
    "实时", "最新", "今天", "明天", "后天", "周末",
]

INFO_QUERY_PATTERNS = [
    re.compile(r'什么是|什么叫|啥是|何为'),
    re.compile(r'怎么(?!样|么|办)'),
    re.compile(r'如何(?!何)'),
    re.compile(r'为什么|为啥|怎么回事情'),
    re.compile(r'(?:请|帮我)?解释(?:一下)?'),
    re.compile(r'(?:有|有哪|列举|列出|告诉我|请问).*(?:什么|哪些|怎么|如何)'),
    re.compile(r'的区别|的差异|对比|比较'),
    re.compile(r'(?:推荐|介绍|说说).*(?:书|电影|音乐|APP|软件|工具|网站)'),
    re.compile(r'你(?:是|叫).{0,6}(?:谁|什么)'),
    re.compile(r'你(?:知道|了解|认识|听说过)'),
    re.compile(r'(?:今年|现在|当前).*(?:几几年|哪年|哪一年|年份)'),
    re.compile(r'(?:几月|几号|哪月|哪天|星期几)'),
    re.compile(r'(?:在|位于|在).*(?:哪|哪里|什么地方)'),
    re.compile(r'是不是|有没有|会不会|能不能|可不可以'),
    re.compile(r'.*吗\s*$'),
]


def _is_info_query(text: str) -> bool:
    for kw in INFO_QUERY_KEYWORDS:
        if kw in text:
            return True
    for p in INFO_QUERY_PATTERNS:
        if p.search(text):
            return True
    return False


# ═══════════════════════════════════════
# 混合意图检测
# ═══════════════════════════════════════

def _detect_mixed(text: str) -> RouteDecision | None:
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)
    has_xiaoai = bool(device) or bool(music) or is_scene

    has_connector = any(kw in text for kw in ["然后", "并且", "同时", "还有", "再", "也", "又"])
    is_info = _is_info_query(text)
    is_creative = any(kw in text for kw in ["写", "创作", "生成", "画", "编", "作曲"])

    if has_xiaoai and (is_info or is_creative) and has_connector:
        sub_tasks = [
            {"path": "xiaoai", "device_actions": device, "music_action": music,
             "scene": any(kw in text for kw in SCENE_KEYWORDS)},
            {"path": "llm", "text": text},
        ]
        return RouteDecision(
            path="mixed", confidence=0.8, reason="混合意图: 设备+大模型",
            sub_tasks=sub_tasks, device_actions=device, music_action=music,
        )
    return None


# ═══════════════════════════════════════
# CET-6 备考检测
# ═══════════════════════════════════════

def _detect_cet6(text: str) -> bool:
    t = text.lower()
    # 内联 STT 纠错（捕获未被全局 CORRECTIONS 覆盖的边界情况）
    t = t.replace("真体", "真题").replace("真旗", "真题").replace("六旗", "六级")
    has_cet = any(kw in t for kw in ["六级", "cet6", "cet-6", "英语六级", "四六级"])
    has_study = any(kw in t for kw in [
        "备考", "复习", "学习", "真题", "做题", "练习", "考试",
        "模拟", "刷题", "卷子", "做卷", "测试", "做",
        "听力", "口语", "阅读", "写作", "翻译", "完形",
        "背单词", "做卷子",
    ])

    # 组合正则：捕获"想/要/来/给 + 做/弄/搞/刷 + ... + 六级/cet6/真题"
    composite_re = re.compile(r'(?:想|要|来|给)(?:做|弄|搞|刷).*(?:六级|cet6|真题)')
    if composite_re.search(t):
        return True

    # 音乐命令过滤：避免"播放六级听力"被误路由到 cet6
    music_start = any(t.startswith(kw) for kw in ["播放", "听", "唱", "放", "来首", "来一"])
    strong_study = any(kw in t for kw in ["真题", "做题", "备考", "试卷", "做卷", "刷题",
                                           "阅读", "写作", "翻译", "复习", "练习"])
    if has_cet and music_start and not strong_study:
        return False

    if has_cet and has_study:
        return True

    if has_cet and len(t) <= 6 and not any(kw in t for kw in ["播放", "听", "唱", "放"]):
        return True

    return False


# ═══════════════════════════════════════
# 设备检测
# ═══════════════════════════════════════

def _check_xiaoai(text: str) -> RouteDecision | None:
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)
    has_actionable = bool(device) or bool(music) or is_scene

    if has_actionable:
        # 仅音乐意图 + 长文本 + 关键词不在开头 → 交给 LLM
        if music and not device and not is_scene and len(text) > 20:
            first_pos = min(
                (text.find(kw) for kw in MUSIC_ACTIONS["play"] if kw in text),
                default=-1,
            )
            if first_pos > 8:
                return None

        parts = []
        if device:
            parts.append(f"设备:{len(device)}项")
        if music:
            parts.append(f"音乐:{music['action']}")
        if is_scene:
            parts.append("场景")
        return RouteDecision(
            path="xiaoai", confidence=0.9, reason=" | ".join(parts),
            matched_key=" | ".join(parts),
            device_actions=device, music_action=music,
        )

    for kw in XIAOAI_QUERY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"查询: {kw}", matched_key="查询")
    for kw in XIAOAI_UTILITY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"工具: {kw}", matched_key="工具")

    return None


# ═══════════════════════════════════════
# 音乐串扰检测（精简版）
# ═══════════════════════════════════════

MUSIC_BLEED_PATTERNS = [
    re.compile(r'字幕\s*by', re.IGNORECASE),
    re.compile(r'^[a-zA-Z\s]+$'),                   # 全英文
    re.compile(r'^(谢谢|謝謝|感謝|thank|thanks)\s*$', re.IGNORECASE),  # 感谢/结束语
    # 短英文噪音（环境噪音被 SenseVoice 转写为 "I.", "The.", "am." 等）
    re.compile(r'^[a-zA-Z]{1,4}\.?$'),              # 单英文单词 + 可选句号
    re.compile(r'^[a-zA-Z\s]{1,10}\.?$'),           # 极短英文短语（< 10 字符纯 ASCII）
]


def _is_music_bleed(text: str) -> bool:
    for p in MUSIC_BLEED_PATTERNS:
        if p.search(text):
            return True
    # 短文本 + 重复字符
    if len(text) <= 6:
        chars = set(text)
        if len(chars) <= 2:
            if any(text.count(c) >= 2 for c in chars):
                return True
    return False


# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

def classify(text: str) -> RouteDecision:
    """
    六类路由主入口。

    路由顺序:
      0. 音乐串扰过滤 → noise
      1. 强制走大模型前缀 → llm (force_llm=True)
      2. Reasonix 编程任务 → reasonix
      3. 混合意图 → mixed
      4. 设备可执行 → xiaoai
      5. CET-6 备考 → cet6
      6. 信息查询 → info_query
      7. 大模型兜底 → llm
    """
    text = text.strip()
    if not text:
        return RouteDecision(path="llm", confidence=0.5, reason="空文本", matched_key="默认")

    if _is_music_bleed(text):
        return RouteDecision(path="noise", confidence=0.95, reason="疑似音乐串扰", matched_key="噪声过滤")

    if _check_force_llm(text):
        return RouteDecision(
            path="llm", confidence=0.9, reason="用户要求走大模型",
            matched_key="force_llm", force_llm=True,
        )

    d = _check_reasonix(text)
    if d:
        return d

    d = _detect_mixed(text)
    if d:
        return d

    d = _check_xiaoai(text)
    if d:
        return d

    if _detect_cet6(text):
        return RouteDecision(
            path="cet6", confidence=0.85, reason="CET-6 备考", matched_key="cet6",
        )

    if _is_info_query(text):
        return RouteDecision(
            path="info_query", confidence=0.7, reason="信息查询", matched_key="信息查询",
        )

    return RouteDecision(path="llm", confidence=0.5, reason="大模型兜底", matched_key="默认")
