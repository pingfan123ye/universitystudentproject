"""
大模型调用服务 —— 双引擎调度器
封装 Ollama（本地）和 DeepSeek（云端），根据配置自动切换。
"""
import asyncio
import json
import logging
import re
from typing import AsyncIterator

import ollama

from services.engine_config import get_config
from services import deepseek_provider

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "qwen2.5:7b"

# 可用设备列表（注入提示词）
DEVICE_LIST = """- 灯: 卧室灯、客厅灯、厨房灯、卫生间灯、书房灯 (打开/关闭)
- 窗帘: 客厅窗帘 (拉开/关上)
- 空调: 可调温度 (打开/关闭/调高/调低)
- 热水器: (打开/关闭)"""

SYSTEM_PROMPT = f"""你是"小智"，一个智能语音助手。你可以协同「Reasonix」一起为用户服务。

你有两个协同伙伴：
1. **你自己（大模型）**：负责思考、推理、聊天、回答问题
2. **Reasonix**：负责编程、写代码、执行命令行操作（通过「工作助手」「克劳德」「贾维斯」触发）

当前可用的虚拟设备：
{DEVICE_LIST}

你可以做的事情：
- 控制上述设备（用户说"打开卧室灯"，你去执行）
- 播放音乐、暂停、下一首、音量调节

【音乐控制指令 — 非常重要】
当用户想听歌时，输出音乐操作标签：
- 播放指定歌曲 → [ACTIONS]{{"music":{{"action":"play","query":"<歌名或歌手>"}}}}[/ACTIONS]
  例：用户说"播放夜空中最亮的星" → query 填 "夜空中最亮的星"
  例：用户说"来首周杰伦的歌" → query 填 "周杰伦"
- 切歌/下一首/换一首 → [ACTIONS]{{"music":{{"action":"next"}}}}[/ACTIONS]
- 上一首 → [ACTIONS]{{"music":{{"action":"prev"}}}}[/ACTIONS]
- 暂停/别放了 → [ACTIONS]{{"music":{{"action":"pause"}}}}[/ACTIONS]
- 继续播放 → [ACTIONS]{{"music":{{"action":"resume"}}}}[/ACTIONS]
- 泛化请求（没指定歌名，如"放点音乐""来首歌""想听歌"）→ query 留空 ""

注意：如果用户的语音指令可能被 Whisper 识别错误（如"星天"可能是"晴天"），请自行纠正为最可能的歌曲名。

回复规则（非常重要）：
0. **输出格式**：你的回复由两部分组成——
   **可见文字**（会被 TTS 朗读） + **隐藏指令标签**（系统自动移除，用户永远不会看到）

   可见文字规则：
   - **绝对禁止**使用任何 emoji 表情符号（如 😊🎵🎶❤️🔊 等）
   - **绝对禁止**使用 Markdown 格式（如 **粗体**、`代码`、# 标题 等）
   - 回复只包含自然口语文字，就像你在跟人对话

   隐藏指令规则：
   - 当你需要播放音乐或控制设备时，在回复**开头**输出 [ACTIONS]...[/ACTIONS] 标签
   - [ACTIONS] 标签是内部指令，会被系统自动移除并执行，用户绝不会看到或听到
   - 如果你不需要执行任何操作（纯闲聊/问答），就不要输出 [ACTIONS] 标签
1. 闲聊/问答/确认状态 → 直接回复即可，**不要**输出任何 [ACTIONS] 标签。对于包含"音乐""歌"但非指令性的句子（如"你喜欢音乐吗""这首歌好听吗"），只做文字回复，绝不输出 [ACTIONS]。
2. 用户明确要求播放音乐 → 用 [ACTIONS]{{"music":{{"action":"play","query":"歌曲名 歌手"}}}}[/ACTIONS] 标记
   - 用户说"播放夜空中最亮的星" → query 填 "夜空中最亮的星"
   - 用户说"来首歌" / "放点音乐" / "想听歌" / "听歌" / "我有点想听歌" / "放首歌"但没指定歌名 → query 留空 ""
   - **绝不要**在闲聊回复中因为提到"听歌"两个字就加 [ACTIONS]
3. 用户明确要求控制设备 → 用 [ACTIONS]{{"devices":[{{"device":"bedroom_light","action":"on"}}]}}[/ACTIONS] 标记
4. 暂停/下一首/上一首/切歌 → [ACTIONS]{{"music":{{"action":"pause"}}}}[/ACTIONS] 等
5. 用户要求写代码 → 回复"正在生成代码，请说允许开始执行"（不要输出技术细节）
6. 回复简洁自然，一般不超过 80 字，像日常朋友聊天一样
7. 不要虚构实时信息（天气、新闻等），不确定就诚实说明
8. 如果你需要联网获取实时信息来回答，可以用 [SEARCH]搜索关键词[/SEARCH] 标记，系统会自动搜索并将结果替换到回复中
9. 【版权歌曲 — 非常重要】对于知名商业流行歌曲（如周杰伦、林俊杰、邓紫棋等华语热门歌手），除非你确定系统能合法播放（本地歌单中有），否则**不要输出 [ACTIONS] 标签**。直接告知用户无法播放有版权的歌曲，建议把mp3放到music目录。
10. 【歌单播放】当用户请求播放某类情绪/场景/风格歌曲、或未指定歌名的泛化播放请求时：
   - **必须**从【当前可用歌单】列表中**原样复制**一个歌单名到 playlist 字段
   - **绝对禁止**自行创作、翻译、改写、或编造歌单名
   - 参考映射（当用户意图不直接等于歌单名时，按以下规则选择）：
     * 学习/专注/看书/写作业/备考/焦虑/紧张 → 选包含"轻音乐""专注""学习"关键词的歌单
     * 助眠/睡觉/失眠/休息/冥想/放松 → 选包含"轻音乐""助眠""放松"关键词的歌单
     * 日常闲聊/随便放/来点音乐/放首歌（无具体歌名）→ 优先选"收藏"，其次选第一个歌单
     * 用户明确说了歌单名中的关键词 → 选最匹配的歌单
   - 输出格式：[ACTIONS]{{\"music\":{{\"action\":\"play\",\"playlist\":\"<从列表中复制的精确歌单名>\"}}}}[/ACTIONS]
   - **不确定时**：宁可空着 playlist 让后端自己选，也**绝不编造**

11. 【学习/工作计划模板】当用户要求制定学习或工作计划时，按以下模板组织回复：

   核心原则：学习计划的核心是"专注攻克弱点"，不是"放松"。音乐辅助是为了屏蔽干扰、帮助进入专注状态。

   时间拆分（基准1小时，等比缩放到其他时长）：
   · 前~35%：攻克今天最难的一道题/一段阅读/一个概念 —— 不追求完美，先把硬骨头啃下来
   · 中间~50%：做题/实践 + 错题回看 —— 用练习检验理解，标记卡住的地方
   · 最后~15%：把错因写成一句话备忘 —— 这是最容易被跳过但最有长期价值的环节

   其他时长参考：
   · 30分钟 → 10min攻克难点 + 15min做题回顾 + 5min备忘
   · 2小时 → 40min攻克难点 + 60min做题回顾 + 20min备忘与休息
   · 3小时以上 → 拆成多轮，每轮含攻克+做题+备忘，轮间休息5-10分钟

   回复结构（必须包含以下四部分，缺一不可）：
   1. 共情开头（1句）：先回应用户的情绪状态。如"焦虑其实是好事，说明你在乎这件事——来，我帮你把接下来一小时理清楚"
   2. 计划拆分（主体）：逐段说明每个时间块做什么、为什么这样安排、有什么小技巧
   3. 音乐说明（1句）：提到已播放的歌单，如"轻音乐已经在放了，不吵不闹，刚好帮你进入状态"
   4. 结尾鼓励（1句）：有温度的收尾。如"先把最难的啃完，后面就轻松了。我在呢，有问题随时叫我"

   风格要求：口语自然、像学长或朋友在帮你规划、有温度不冰冷、不用emoji、不用markdown、不要念模板感。"""


# ACTIONS 正则（同时匹配方括号 [ 和中文书名号 【 ）
ACTIONS_RE = re.compile(r'[\[【]ACTIONS[]】](.*?)[\[【]/ACTIONS[]】]', re.DOTALL)
SEARCH_TAG_RE = re.compile(r'\[SEARCH\].*?\[/SEARCH\]', re.DOTALL)


def strip_search_tags(text: str) -> str:
    """去除回复中的 [SEARCH]...[/SEARCH] 标签"""
    return SEARCH_TAG_RE.sub('', text).strip()


def _try_fix_json(raw: str) -> str | None:
    """尝试修复 LLM 常见的 JSON 格式错误，修复成功返回修复后字符串，否则返回 None"""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    fixes_tried = []
    # 1. 缺失结尾 }
    if s.count('{') > s.count('}'):
        fixed = s + ('}' * (s.count('{') - s.count('}')))
        fixes_tried.append(fixed)
    # 2. 缺失结尾 ]
    if s.count('[') > s.count(']'):
        fixed = s + (']' * (s.count('[') - s.count(']')))
        fixes_tried.append(fixed)
    # 3. 首尾多了引号（LLM 偶尔会把整个 JSON 包在引号里）
    if s.startswith('"') and s.endswith('"') and len(s) > 2:
        fixes_tried.append(s[1:-1])
    # 4. 尾随逗号（在 } 或 ] 之前）
    import re as _re
    fixes_tried.append(_re.sub(r',(\s*[}\]])', r'\1', s))
    # 5. 原始字符串也试一次
    if s not in fixes_tried:
        fixes_tried.append(s)
    for candidate in fixes_tried:
        try:
            json.loads(candidate)
            return candidate
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_actions(text: str) -> tuple[str, dict | None]:
    m = ACTIONS_RE.search(text)
    if m:
        raw_json = m.group(1)
        clean = ACTIONS_RE.sub('', text).strip()
        # 先直接解析
        try:
            actions = json.loads(raw_json)
            return clean, actions
        except json.JSONDecodeError:
            pass
        # JSON 解析失败，尝试自动修复（小模型常见输出不完整 JSON）
        fixed = _try_fix_json(raw_json)
        if fixed:
            try:
                actions = json.loads(fixed)
                logger.warning(f"ACTIONS JSON 自动修复成功: {raw_json[:80]} → {fixed[:80]}")
                return clean, actions
            except json.JSONDecodeError:
                pass
        # 彻底失败：记录警告，但**仍然剥离标签**，避免泄露到用户界面
        logger.warning(f"ACTIONS JSON 解析失败（已剥离标签）: {raw_json[:100]}")
        return clean, None
    return text, None


# ===== Ollama 本地提供者 =====

async def _ollama_stream(
    messages: list[dict],
    model: str = DEFAULT_LOCAL_MODEL,
    actions_out: list[str] | None = None,
) -> AsyncIterator[str]:
    """Ollama 流式调用

    Args:
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签内容会追加到此列表。
                     调用方可在流结束后读取并执行音乐/设备操作。
    """
    try:
        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": 0.7, "top_p": 0.9},
        )
        buffer = ""
        in_tag = False
        in_search = False
        open_marker = ""   # 记录当前 ACTIONS 开始标记，用于配对闭合标签

        for chunk in stream:
            if chunk and "message" in chunk and "content" in chunk["message"]:
                token = chunk["message"]["content"]
                if not token:
                    continue
                buffer += token

                # [SEARCH] 标签检测：在标签内不输出
                if not in_search and "[SEARCH]" in buffer:
                    in_search = True
                if in_search:
                    if "[/SEARCH]" in buffer:
                        tag_end = buffer.find("[/SEARCH]") + len("[/SEARCH]")
                        buffer = buffer[tag_end:]
                        in_search = False
                    continue

                # [ACTIONS] 标签检测：在标签内不输出（但保存到 actions_out）
                if not in_tag and ("[ACTIONS]" in buffer or "【ACTIONS】" in buffer):
                    open_marker = "[ACTIONS]" if "[ACTIONS]" in buffer else "【ACTIONS】"
                    tag_start = buffer.find(open_marker)
                    before = buffer[:tag_start]
                    if before:
                        yield before
                    buffer = buffer[tag_start:]
                    in_tag = True
                if in_tag:
                    close_marker = "[/ACTIONS]" if open_marker == "[ACTIONS]" else "【/ACTIONS】"
                    if close_marker in buffer:
                        tag_end = buffer.find(close_marker) + len(close_marker)
                        # 保存完整 ACTIONS 标签到侧通道（供 call_llm 读取并执行）
                        if actions_out is not None:
                            actions_out.append(buffer[:tag_end])
                        buffer = buffer[tag_end:]
                        in_tag = False
                        open_marker = ""
                    continue

                # 正常流式输出
                if buffer and not in_tag and not in_search:
                    yield buffer
                    buffer = ""

        # 流结束后 flush 剩余缓冲区
        if buffer and not in_tag and not in_search:
            clean, _ = parse_actions(buffer)
            if clean:
                yield strip_search_tags(clean)
            else:
                stripped = strip_search_tags(buffer)
                if stripped:
                    yield stripped
        # 若流结束时仍在 ACTIONS 标签内（模型异常截断），也尝试保存
        if buffer and in_tag and open_marker and actions_out is not None:
            actions_out.append(buffer)

    except ollama.ResponseError as e:
        raise RuntimeError(f"Ollama 模型调用失败: {e.error}") from e
    except Exception as e:
        raise RuntimeError(f"Ollama 服务异常: {str(e)}") from e


# ===== DeepSeek 云端提供者 =====

async def _deepseek_stream(
    messages: list[dict],
    model: str = "deepseek-v4-flash",
    enable_search: bool = False,
    actions_out: list[str] | None = None,
) -> AsyncIterator[str]:
    """DeepSeek 流式调用，过滤 [SEARCH] / [ACTIONS] 标签

    Args:
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签内容会追加到此列表。
                     调用方可在流结束后读取并执行音乐/设备操作。
    """
    buffer = ""
    in_tag = False
    in_search = False
    open_marker = ""   # 记录当前 ACTIONS 开始标记，用于配对闭合标签

    async for token in deepseek_provider.generate_stream(
        messages=messages,
        model=model,
        enable_search=enable_search,
    ):
        if not token:
            continue
        buffer += token

        # [SEARCH] 标签检测：在标签内不输出
        if not in_search and "[SEARCH]" in buffer:
            in_search = True
        if in_search:
            if "[/SEARCH]" in buffer:
                tag_end = buffer.find("[/SEARCH]") + len("[/SEARCH]")
                buffer = buffer[tag_end:]
                in_search = False
            continue

        # [ACTIONS] 标签检测：在标签内不输出（但保存到 actions_out）
        if not in_tag and ("[ACTIONS]" in buffer or "【ACTIONS】" in buffer):
            open_marker = "[ACTIONS]" if "[ACTIONS]" in buffer else "【ACTIONS】"
            tag_start = buffer.find(open_marker)
            before = buffer[:tag_start]
            if before:
                yield before
            buffer = buffer[tag_start:]
            in_tag = True
        if in_tag:
            close_marker = "[/ACTIONS]" if open_marker == "[ACTIONS]" else "【/ACTIONS】"
            if close_marker in buffer:
                tag_end = buffer.find(close_marker) + len(close_marker)
                # 保存完整 ACTIONS 标签到侧通道（供 call_llm 读取并执行）
                if actions_out is not None:
                    actions_out.append(buffer[:tag_end])
                buffer = buffer[tag_end:]
                in_tag = False
                open_marker = ""
            continue

        # 正常流式输出
        if buffer and not in_tag and not in_search:
            yield buffer
            buffer = ""

    # 流结束后 flush 剩余缓冲区
    if buffer and not in_tag and not in_search:
        clean, _ = parse_actions(buffer)
        if clean:
            yield strip_search_tags(clean)
        else:
            stripped = strip_search_tags(buffer)
            if stripped:
                yield stripped
    # 若流结束时仍在 ACTIONS 标签内（模型异常截断），也尝试保存
    if buffer and in_tag and open_marker and actions_out is not None:
        actions_out.append(buffer)


# ===== 双引擎调度入口 =====

async def generate_stream(
    prompt: str,
    model: str = DEFAULT_LOCAL_MODEL,
    memory_context: str = "",
    conversation_history: list[dict] | None = None,
    prefer_cloud: bool = False,           # 调用方要求优先云端
    model_used: list[str] | None = None,  # 传出参数：实际使用的模型名
    actions_out: list[str] | None = None, # 侧通道：收集流式中的 [ACTIONS] 标签
) -> AsyncIterator[str]:
    """
    根据引擎配置自动选择本地或云端模型。

    Args:
        prompt: 用户输入
        model: 本地模型名
        memory_context: 长期记忆上下文
        conversation_history: 对话历史
        prefer_cloud: 是否优先使用云端（info_query/mixed 路径传入 True）
        model_used: 传出参数，调用后包含实际使用的模型名
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签会追加到此列表
    """
    config = get_config()
    enable_search = config.get("enable_search", True)

    # 构建 messages
    system_msg = SYSTEM_PROMPT
    if memory_context:
        system_msg += "\n" + memory_context
    messages = [{"role": "system", "content": system_msg}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    # ── 决策：用云端还是本地 ──
    use_cloud = False
    cloud_model = config.get("cloud_model")
    cloud_available = deepseek_provider.is_available()

    if prefer_cloud and cloud_available:
        use_cloud = True
    else:
        mode = config.resolve_mode(prompt)
        use_cloud = (mode == "cloud") and cloud_available

    # ── 云端路径 ──
    if use_cloud:
        logger.info(f"双引擎 → 云端 (model={cloud_model}, search={enable_search})")
        if model_used is not None:
            model_used.append(f"deepseek:{cloud_model}")
        try:
            async for token in _deepseek_stream(
                messages=messages,
                model=cloud_model,
                enable_search=enable_search,
                actions_out=actions_out,
            ):
                yield token
            return
        except Exception as e:
            logger.warning(f"云端失败，切回本地: {e}")
            # 降级到本地

    # ── 本地路径 ──
    logger.info(f"双引擎 → 本地 (model={model})")
    if model_used is not None:
        model_used.append(f"ollama:{model}")

    # 本地调用（直接流式输出，异常时切云端）
    try:
        async for token in _ollama_stream(messages, model, actions_out=actions_out):
            yield token
    except RuntimeError as e:
        # Ollama 错误，检查是否可切云端
        if deepseek_provider.is_available():
            logger.warning(f"Ollama 失败，切到云端: {e}")
            if model_used is not None and len(model_used) > 0:
                model_used[-1] = f"deepseek:{config.get('cloud_model')}"
            # 清除失败调用可能收集到的部分/损坏 ACTIONS
            if actions_out is not None:
                actions_out.clear()
            async for token in _deepseek_stream(
                messages=messages,
                model=config.get("cloud_model"),
                enable_search=enable_search,
                actions_out=actions_out,
            ):
                yield token
        else:
            raise


# ===== 健康检查 =====

async def check_model_available(model: str = DEFAULT_LOCAL_MODEL) -> bool:
    """检查本地 Ollama 模型是否可用"""
    try:
        models = ollama.list()
        model_names = [m.get("name", "") for m in models.get("models", [])]
        return any(model in name or name in model for name in model_names)
    except Exception:
        return False


async def check_deepseek_available() -> bool:
    """检查 DeepSeek API 是否可用"""
    return await deepseek_provider.check_available()


# 兼容旧导入名
DEFAULT_MODEL = DEFAULT_LOCAL_MODEL
