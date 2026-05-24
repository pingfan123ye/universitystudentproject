"""
意图路由分发器 —— 五类路由 + 混合意图拆分

路由路径:
  reasonix  → 编程/工作助手
  xiaoai    → 设备控制/音乐/场景（小爱可完成）
  info_query → 信息查询（天气/新闻/百科等，进双引擎调度）
  mixed     → 混合意图（拆分子任务分别路由）
  llm       → 大模型兜底（含强制走大模型）
"""
import re
from dataclasses import dataclass, field


@dataclass
class RouteDecision:
    path: str               # reasonix | xiaoai | info_query | mixed | llm
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
    """用户明确要求不要小爱，直接走大模型"""
    for p in FORCE_LLM_PREFIX:
        if p.search(text):
            return True
    # 一些模糊表达也当作强制走大模型
    if text.startswith("你") and ("觉得" in text or "认为" in text or "建议" in text or "推荐" in text):
        return True
    return False


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

SCENE_KEYWORDS = ["离家模式", "回家模式", "晚安", "起床模式", "观影模式", "阅读模式"]

# 小爱可处理的基础查询（仅保留虚拟设备能回答的）
XIAOAI_QUERY = ["时间", "几点"]
XIAOAI_UTILITY = ["闹钟", "提醒", "倒计时", "计时"]


def _detect_music_action(text: str) -> dict | None:
    for kw in MUSIC_ACTIONS["pause"]:
        if kw in text:
            return {"action": "pause"}
    for kw in MUSIC_ACTIONS["play"]:
        if kw in text:
            return {"action": "play"}
    for kw in MUSIC_ACTIONS["next"]:
        if kw in text:
            return {"action": "next"}
    for kw in MUSIC_ACTIONS["prev"]:
        if kw in text:
            return {"action": "prev"}
    if any(kw in text for kw in VOLUME_KEYWORDS):
        return {"action": "play"}
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

# 明确需要实时/外部信息的查询
INFO_QUERY_KEYWORDS = [
    "天气", "新闻", "头条", "热搜", "股票", "汇率", "油价",
    "实时", "最新", "今天", "明天", "后天", "周末",
]

# 百科/知识类询问句式
INFO_QUERY_PATTERNS = [
    re.compile(r'什么是|什么叫|什么是|啥是|何为'),          # 定义类
    re.compile(r'怎么(?!样|么|办)'),                         # 方法类（排除"怎么样""怎么办"）
    re.compile(r'如何(?!何)'),                               # 方法类
    re.compile(r'为什么|为啥|怎么回事情'),                   # 原因类
    re.compile(r'(?:请|帮我)?解释(?:一下)?'),                # 解释类
    re.compile(r'(?:有|有哪|列举|列出|告诉我|请问).*(?:什么|哪些|怎么|如何)'),
    re.compile(r'的区别|的差异|对比|比较'),                  # 比较类
    re.compile(r'(?:推荐|介绍|说说).*(?:书|电影|音乐|APP|软件|工具|网站)'),
    # 新增：常见问句
    re.compile(r'你(?:是|叫).{0,6}(?:谁|什么)'),            # 你是谁/你叫什么
    re.compile(r'你(?:知道|了解|认识|听说过)'),             # 你知道...吗
    re.compile(r'(?:今年|现在|当前).*(?:几几年|哪年|哪一年|年份)'),  # 年份查询
    re.compile(r'(?:几月|几号|哪月|哪天|星期几)'),           # 日期查询
    re.compile(r'(?:在|位于|在).*(?:哪|哪里|什么地方)'),     # 位置查询
    re.compile(r'是不是|有没有|会不会|能不能|可不可以'),     # 是否类
    re.compile(r'.*吗\s*$'),                                 # 一般疑问句（以"吗"结尾）
]


def _is_info_query(text: str) -> bool:
    """检测是否为信息查询（天气/新闻/百科/知识等）"""
    # 先查关键词
    for kw in INFO_QUERY_KEYWORDS:
        if kw in text:
            return True
    # 再查句式
    for p in INFO_QUERY_PATTERNS:
        if p.search(text):
            return True
    return False


# ═══════════════════════════════════════
# 混合意图检测
# ═══════════════════════════════════════

def _detect_mixed(text: str) -> RouteDecision | None:
    """
    检测混合意图：既有小爱操作，又有信息查询/创作需求。
    例如："写首诗然后播放音乐" → 拆分
         "打开灯帮我查一下明天天气" → 拆分
    """
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)
    has_xiaoai = bool(device) or bool(music) or is_scene

    # 双连接词（然后/并且/同时/还有） + 两类不同操作 → 混合
    has_connector = any(kw in text for kw in ["然后", "并且", "同时", "还有", "再", "也", "又"])
    is_info = _is_info_query(text)
    is_creative = any(kw in text for kw in ["写", "创作", "生成", "画", "编", "作曲"])

    if has_xiaoai and (is_info or is_creative) and has_connector:
        # 拆分：小爱部分 + 大模型部分
        sub_tasks = []

        # 小爱子任务
        xiaoai_task = {
            "path": "xiaoai",
            "device_actions": device,
            "music_action": music,
            "scene": any(kw in text for kw in SCENE_KEYWORDS),
        }
        sub_tasks.append(xiaoai_task)

        # 大模型子任务（剩余的文本）
        llm_task = {"path": "llm", "text": text}
        sub_tasks.append(llm_task)

        return RouteDecision(
            path="mixed",
            confidence=0.8,
            reason="混合意图: 小爱+大模型",
            sub_tasks=sub_tasks,
            device_actions=device,
            music_action=music,
        )

    return None


# ═══════════════════════════════════════
# 小爱检测（纯设备/音乐/场景）
# ═══════════════════════════════════════

def _check_xiaoai(text: str) -> RouteDecision | None:
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)
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

    # 小爱能回答的简单查询
    for kw in XIAOAI_QUERY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"查询: {kw}", matched_key="查询")
    for kw in XIAOAI_UTILITY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"工具: {kw}", matched_key="工具")

    return None


# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

def classify(text: str) -> RouteDecision:
    """
    五类路由主入口。

    路由顺序:
      1. 强制走大模型前缀 → llm (force_llm=True)
      2. Reasonix 编程任务 → reasonix
      3. 混合意图（既有小爱又有大模型需求）→ mixed
      4. 小爱可执行 → xiaoai
      5. 信息查询（天气/百科等，进双引擎）→ info_query
      6. 大模型兜底 → llm
    """
    text = text.strip()
    if not text:
        return RouteDecision(path="llm", confidence=0.5, reason="空文本", matched_key="默认")

    # 1. 强制走大模型
    if _check_force_llm(text):
        return RouteDecision(
            path="llm", confidence=0.9, reason="用户要求走大模型",
            matched_key="force_llm", force_llm=True,
        )

    # 2. Reasonix
    d = _check_reasonix(text)
    if d:
        return d

    # 3. 混合意图
    d = _detect_mixed(text)
    if d:
        return d

    # 4. 小爱（用户没要求走大模型）
    d = _check_xiaoai(text)
    if d:
        return d

    # 5. 信息查询（进双引擎调度）
    if _is_info_query(text):
        return RouteDecision(
            path="info_query", confidence=0.7, reason="信息查询",
            matched_key="信息查询",
        )

    # 6. 大模型兜底
    return RouteDecision(path="llm", confidence=0.5, reason="大模型兜底", matched_key="默认")
