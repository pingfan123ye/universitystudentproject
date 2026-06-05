"""
意图路由分发器 —— 五类路由 + 混合意图拆分

路由路径:
  reasonix  → 编程/工作助手
  xiaoai    → 设备控制/音乐/场景
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
    """用户明确要求直接走大模型"""
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

MUSIC_ACTIONS = {
    "play":  ["播放音乐", "播放歌曲", "放歌", "放音乐", "放点音乐", "放个音乐",
              "来首歌", "唱歌", "唱首歌", "听歌", "放一首", "播一首", "播音乐", "播歌",
              "放背景音乐", "播放背景音乐", "播放轻音乐", "来点音乐", "来首",
              "放轻松音乐", "放点歌", "放首歌", "来点歌", "播点音乐", "播点歌",
              "来段音乐", "想听音乐", "想听歌", "放个歌", "给我放", "给我唱",
              "来点背景音乐", "放些音乐", "放些歌", "播一下音乐", "播一下歌",
              # 简化匹配：具体歌曲名引用
              "播放", "我想听", "想听", "我要听", "给我放", "放一个", "放个",
              "唱一个", "点歌", "点一首", "来一个", "来一曲", "来一首",
              # 繁體中文（Whisper 可能输出繁体）
              "聽歌", "聽音樂", "想聽", "我想聽", "我要聽", "來首歌", "唱首歌",
              "放音樂", "放個音樂", "放點音樂", "來點音樂", "播音樂", "播歌",
              "播放音樂", "播放歌曲", "點歌", "點一首", "來一曲", "給我放", "給我唱",
              # 随机/泛化请求
              "随便来一首", "随便来一个", "随便来个", "随便放", "随便播", "随便放一首",
              "随机播放", "随机来", "随机来一首", "随意来一首", "随意放",
              "随便什么", "都可以", "什么都行", "什么都可", "来点随机的", "来点随便的",
              # 短关键词：匹配 "放点不吵的学习歌" / "来点轻音乐" 等自然语言（作为 play 意图入口）
              "放点", "放些", "来点", "播点", "来些", "播些", "放个",
              "想听点", "想聽點", "想听些", "想聽些", "来首", "來首"],
    "pause": ["暂停音乐", "暂停播放", "暂停", "停止播放", "停止音乐",
              "关掉音乐", "关闭音乐", "关音乐", "停掉音乐", "别放了", "别唱了",
              "停了音乐", "把音乐关了", "把音乐关掉", "关背景音乐", "别播了", "别放了",
              # 繁體中文
              "暫停音樂", "暫停播放", "暫停", "關掉音樂", "關閉音樂", "關音樂",
              "停掉音樂", "別放了", "別唱了", "把音樂關了", "把音樂關掉", "別播了"],
    "next":  ["下一首", "下一首歌", "下一曲", "换一首", "切歌", "切割", "换首歌", "换个歌", "切一首",
              # 繁體中文
              "下一首", "下一首歌", "下一曲", "換一首", "切歌", "切割", "換首歌", "換個歌", "切一首"],
    "prev":  ["上一首", "上一曲"],
    "resume": ["继续播放", "继续", "恢复播放",
               # 繁體中文
               "繼續播放", "繼續", "恢復播放"],
    "stop": ["停了", "停止", "关掉音乐", "关闭音乐",
             # 繁體中文
             "停了", "關掉音樂", "關閉音樂"],
}

VOLUME_KEYWORDS = ["音量", "大声", "小声", "静音", "声音"]

SCENE_KEYWORDS = ["离家模式", "回家模式", "晚安", "起床模式", "观影模式", "阅读模式"]

# 严格的音乐播放意图正则：必须包含 动作词 + 歌名/描述
# "你喜欢音乐吗"、"听歌识曲" → 不匹配；"播放夜空中最亮的星" → 匹配
MUSIC_PLAY_PATTERNS = [
    re.compile(r"(播放|放|来一首|我想听|我要听|点一首|给我放|给我播|我让你听|我让你放|让你听|让你放)\s*[一-龥a-zA-Z0-9]+"),
    # 繁体变体
    re.compile(r"(來一首|我想聽|我要聽|點一首|給我放|給我播|我讓你聽|我讓你放|讓你聽|讓你放)\s*[一-龥a-zA-Z0-9]+"),
    re.compile(r"听\s*[一-龥a-zA-Z0-9]+\s*(这首歌|这首)"),
    re.compile(r"聽\s*[一-龥a-zA-Z0-9]+\s*(這首歌|這首)"),
    re.compile(r"播\s*[一-龥a-zA-Z0-9]+"),
    # 泛化音乐请求："想听歌""听音乐""来点音乐""放点歌" 等
    re.compile(r"(想听|想聽|听|聽|来点|來點|放点|放點|播点|播點|来首|來首)\s*(歌|音乐|音樂|歌曲|什么歌|啥歌)"),
    # 间隔式音乐请求："放点不吵的学习歌"、"来点轻松的轻音乐"、"放些安静的背景音乐"
    re.compile(r"(放点|放些|来点|来些|播点|播些|想听点|想聽點).{0,15}(歌|音乐|音樂|歌曲|听的|聽的)"),
    # "顺便放点...歌" / "再放点...音乐" —— 兼容器/修饰词在中间的请求
    re.compile(r"(顺便|再|也|还|帮我|给我|帮忙)\s*(放点|放些|来点|播点).{0,15}(歌|音乐|音樂|歌曲)"),
    re.compile(r"(有点|有點|好)\s*(想听|想聽)\s*(歌|音乐|音樂|歌曲)"),
    # 随机/泛化请求："随便什么都可以""什么歌都行""随意来一首"
    re.compile(r"(随便|隨便|随意|隨意|随机|隨機|任意).*(歌|音乐|音樂|歌曲|首|曲|什么|什麼|啥)"),
    re.compile(r"(什么|什麼|啥).*(都可以|都行|也行|都OK|都ok|都可)"),
    re.compile(r"(来|來|放|播|听|聽).*(随便|隨便|随意|隨意|随机|隨機)"),
]


def _is_music_play_intent(text: str) -> bool:
    """只有同时包含动作词+歌名内容才算音乐播放意图"""
    for p in MUSIC_PLAY_PATTERNS:
        if p.search(text):
            return True
    return False

# 设备可处理的基础查询
XIAOAI_QUERY = ["时间", "几点"]
XIAOAI_UTILITY = ["闹钟", "提醒", "倒计时", "计时"]


def _extract_music_query(text: str) -> str:
    """
    从文本中提取歌曲搜索关键词。
    例如：
      "播放周杰伦的晴天" -> "周杰伦 晴天"
      "来一首七里香" -> "七里香"
      "我想听陈奕迅的歌" -> "陈奕迅"
      "我想听歌了请帮我播放歌曲" -> ""（播放本地歌单）
    """
    # 去掉通用音乐关键词前缀（按长度降序匹配最长的）
    play_prefixes = sorted(MUSIC_ACTIONS["play"], key=len, reverse=True)
    query = text
    for p in play_prefixes:
        if p in query:
            idx = query.find(p)
            query = query[idx + len(p):]  # 只取关键词之后的部分（关键词可能在句子中间）
            break
    # 去掉标点和无意义字符
    PUNCT_RE = re.compile(r"[\s,，。！？、；：“”‘’《》!?;:'()]+")
    query = PUNCT_RE.sub(' ', query).strip()
    # 去掉自然语言碎片（不是歌曲名/歌手名的一部分）
    noise = [
        '请帮我', '请帮助', '請幫我', '請幫助', '帮我', '幫我', '给我', '給我',
        '我想', '我要', '请你', '請你', '我让', '我讓', '让你', '讓你',
        '来一首', '來一首', '来一个', '来一段', '来一曲', '来个',
        '一首', '一个', '一段', '一曲',
        '听歌', '聽歌', '听音乐', '聽音樂', '放歌', '放音樂',
        '有点', '有點', '有少少', '想', '歌曲', '一下', '下',
        # 口语填充词/应答词（Whisper 常把语气词转录为这些）
        '好的', '好', '好吧', '行吧', '可以', '那个', '這個', '那个',
        '进行', '一下', '给我', '给我来', '来帮我', '來幫我',
        '选首', '選首', '选个', '選個', '帮我选', '幫我選',
        '选手', '選手', '选手给', '選手給',  # Whisper 常把"选首"误识别为"选手"
        # 随机/泛化请求词
        '随机', '隨機', '随机多', '随便', '隨便', '随意', '隨意', '任意',
        '多放', '多来', '多',  # "随机多放一首" → 多/多放 是修饰词
        '字幕',  # 歌曲歌词被误识别后的常见前缀
    ]
    for w in sorted(noise, key=len, reverse=True):
        query = query.replace(w, ' ')
    # 去掉独立的单字代词（仅当它们前后是空格或边界时才移除，避免误伤歌名如《我》《你》）
    query = re.sub(r'(?:^|\s)[我你他她它](?=\s|$)', ' ', query)
    # 去掉末尾语气词（包括句首虚词）
    query = re.sub(r'^[了吧啊呀哦啦呗么嗯了]+', '', query)
    query = re.sub(r'[了吧啊呀哦啦呗么嗯了]+$', '', query).strip()
    # 去掉末尾的"的歌"、"的音乐"、"的歌曲"、以及单独的"的"
    query = re.sub(r'的(歌|音乐|歌曲)*$', '', query).strip()
    # 去掉唤醒词（可能在任意位置，如"我想听歌了小智"中的"小智"）
    query = re.sub(r'(小智小智|小智|小字小字|小字|小子小子|小子)', ' ', query)
    # 合并多余空格
    query = re.sub(r'\s+', ' ', query).strip()
    # 如果清洗后只剩无意义词，返回空（触发本地歌单/默认播放）
    if query in ("", "歌", "音乐", "音樂", "一首", "个", "點", "下", "首", "曲", "歌曲",
                 "听歌", "聽歌", "听音乐", "聽音樂", "想听", "想聽",
                 "好的", "好", "行", "可以", "行吧", "好吧",
                 "随便", "随意", "随机", "任意", "都可以", "什么都行", "啥都行"):
        return ""
    # 太短（单字）或太长（>40字）不靠谱
    if len(query) < 2 or len(query) > 40:
        return ""
    return query
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
                # 播放意图需要严格匹配：动作词 + 歌名内容
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
    # 纯音量调节不自动触发播放
    if not action and any(kw in text for kw in VOLUME_KEYWORDS):
        if _is_music_play_intent(text):
            action = "play"

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
    检测混合意图：既有设备操作，又有信息查询/创作需求。
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
        # 拆分：设备部分 + 大模型部分
        sub_tasks = []

        # 设备子任务
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
            reason="混合意图: 设备+大模型",
            sub_tasks=sub_tasks,
            device_actions=device,
            music_action=music,
        )

    return None


# ═══════════════════════════════════════
# 设备检测（纯设备/音乐/场景）
# ═══════════════════════════════════════

def _check_xiaoai(text: str) -> RouteDecision | None:
    device = _detect_device_actions(text)
    music = _detect_music_action(text)
    is_scene = any(kw in text for kw in SCENE_KEYWORDS)
    has_actionable = bool(device) or bool(music) or is_scene

    if has_actionable:
        # 仅音乐意图 + 长文本 + 关键词不在开头 → 疑似对话中顺带提音乐
        # 不拦截，留给 LLM 处理（LLM 应通过 [ACTIONS] 标签触发播放，兜底有启发式检测）
        if music and not device and not is_scene and len(text) > 20:
            first_pos = min(
                (text.find(kw) for kw in MUSIC_ACTIONS["play"] if kw in text),
                default=-1,
            )
            if first_pos > 8:
                return None  # 对话为主，音乐在句子后半段 → 交给 LLM

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

    # 设备能回答的简单查询
    for kw in XIAOAI_QUERY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"查询: {kw}", matched_key="查询")
    for kw in XIAOAI_UTILITY:
        if kw in text:
            return RouteDecision(path="xiaoai", confidence=0.5, reason=f"工具: {kw}", matched_key="工具")

    return None


# ═══════════════════════════════════════
# 音乐串扰检测：识别从扬声器播放的歌曲歌词被麦克风误拾取
# ═══════════════════════════════════════

MUSIC_BLEED_PATTERNS = [
    re.compile(r'字幕\s*by', re.IGNORECASE),      # "字幕by索兰娅"
    re.compile(r'by\s*[a-zA-Z一-鿿]+'),    # "by某某" 混中英文
    re.compile(r'^[a-zA-Z\s]+$'),                   # 全英文（不太可能是中文语音指令）
    re.compile(r'^(谢谢|謝謝|感謝|thank|thanks|thank you)\s*$', re.IGNORECASE),  # 纯感谢/结束语
    re.compile(r'^呃[,，]?\s*$'),                    # 纯语气词
]


def _is_music_bleed(text: str) -> bool:
    """检测文本是否像是从播放中的音乐里误拾取的歌词/字幕"""
    for p in MUSIC_BLEED_PATTERNS:
        if p.search(text):
            return True
    # 短文本 + 真正有重复字符（如"謝謝謝謝"、"好的好的"）
    # 避免误杀正常短命令：晚安/开灯/关灯/切歌/暂停 等
    if len(text) <= 6:
        chars = set(text)
        if len(chars) <= 2:
            # 仅当有字符重复出现（非每个字符仅出现一次）才视为噪声
            if any(text.count(c) >= 2 for c in chars):
                return True
    return False


# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

def classify(text: str) -> RouteDecision:
    """
    五类路由主入口。

    路由顺序:
      0. 音乐串扰过滤 → noise（忽略）
      1. 强制走大模型前缀 → llm (force_llm=True)
      2. Reasonix 编程任务 → reasonix
      3. 混合意图（既有设备又有大模型需求）→ mixed
      4. 设备可执行 → xiaoai
      5. 信息查询（天气/百科等，进双引擎）→ info_query
      6. 大模型兜底 → llm
    """
    text = text.strip()
    if not text:
        return RouteDecision(path="llm", confidence=0.5, reason="空文本", matched_key="默认")

    # 0. 音乐串扰过滤：从扬声器误拾取的歌词/字幕 → 静默忽略
    if _is_music_bleed(text):
        return RouteDecision(path="noise", confidence=0.95, reason="疑似音乐串扰", matched_key="噪声过滤")

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

    # 4. 设备（用户没要求走大模型）
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
