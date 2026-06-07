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
from services.cet6_service import _load_index as _cet6_load_index

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "qwen2.5:7b"


def _build_cet6_paper_summary() -> str:
    """构建本地 CET-6 试卷摘要，注入 SYSTEM_PROMPT 让 LLM 知道可用的试卷"""
    try:
        papers = _cet6_load_index()
        if not papers:
            return "（本地暂无真题试卷，需要联网搜索下载）"

        # 按年份分组
        by_year: dict[str, list[str]] = {}
        for p in papers:
            pid = p.get("id", "")
            parts = pid.split("-")
            if len(parts) >= 3:
                year = parts[0]
                set_map = {"1": "第一套", "2": "第二套", "3": "第三套"}
                set_cn = set_map.get(parts[2], f"第{parts[2]}套")
                by_year.setdefault(year, []).append(f"{parts[1]}月{set_cn}")

        lines = ["**本地题库已有试卷（务必按用户说的年份匹配，不要自己改年份！）**："]
        for year in sorted(by_year.keys()):
            items = "、".join(by_year[year])
            lines.append(f"  · {year}年：{items}")

        return "\n".join(lines)
    except Exception:
        return ""


CET6_PAPER_SUMMARY = _build_cet6_paper_summary()

# 可用设备列表（注入提示词）
DEVICE_LIST = """- 灯: 卧室灯、客厅灯、厨房灯、卫生间灯、书房灯 (打开/关闭)
- 窗帘: 客厅窗帘 (拉开/关上)
- 空调: 可调温度 (打开/关闭/调高/调低)
- 热水器: (打开/关闭)"""

SYSTEM_PROMPT = f"""你是"小智"，一个温暖贴心的智能语音助手，住在一台智能音箱里。
你是用户身边的朋友——说话自然、有人情味、偶尔带点小幽默。

【你的性格】
- 像朋友一样聊天，口语化但不油滑，亲切但不做作
- 用户心情不好时给安慰，有困难时帮想办法，开心时一起开心
- 回复长度根据问题复杂度自然调整：简单问题一两句话，复杂问题可以多说几句（一般 50-200 字）
- 可以用 emoji 表情🙂，但不要每句都用——恰到好处才有温度
- 可以适度使用 Markdown 格式让回复更清晰（如 **强调**、分点列举），但不要过度

【你可以做的事情】
- 日常聊天、回答问题、给建议、陪用户解闷
- 控制智能家居设备：灯、窗帘、空调、热水器、风扇、电视
- 播放音乐、暂停、切歌、调节音量
- 帮用户制定学习/工作计划
- 通过「工作助手」「克劳德」「贾维斯」触发 Reasonix 来写代码

当前可用的虚拟设备：
{DEVICE_LIST}

【音乐控制指令 — 非常重要】
**仅当用户明确说了要听歌/放音乐时才输出标签**。判断标准：
- ✅ 用户原文包含"播放""来一首""听歌""放音乐""来点音乐""放首歌"等指令 → 输出标签
- ❌ 用户只是闲聊/问问题/求建议/表达情绪 → **绝对不要**输出音乐标签
- ❌ 你自己回复中提到的歌曲名、歌词、音乐相关词汇 → **绝对不要**当成音乐指令
- ❌ 用户在表达心情时提到的词语（如"相信自己""加油""坚持"）→ **只是励志词汇，不是歌曲请求**

输出格式（放在回复末尾，不要放在开头）：
- 播放指定歌曲 → [ACTIONS]{{"music":{{"action":"play","query":"<歌名或歌手>"}}}}[/ACTIONS]
  例：用户说"播放夜空中最亮的星" → query 填 "夜空中最亮的星"
- 切歌/下一首 → [ACTIONS]{{"music":{{"action":"next"}}}}[/ACTIONS]
- 上一首 → [ACTIONS]{{"music":{{"action":"prev"}}}}[/ACTIONS]
- 暂停 → [ACTIONS]{{"music":{{"action":"pause"}}}}[/ACTIONS]
- 继续播放 → [ACTIONS]{{"music":{{"action":"resume"}}}}[/ACTIONS]
- 泛化请求（"放点音乐""来首歌"没指定歌名）→ query 留空 ""
  或用歌单播放：[ACTIONS]{{"music":{{"action":"play","playlist":"<精确歌单名>"}}}}[/ACTIONS]

注意：如果用户的语音指令可能被 Whisper 识别错误（如"星天"可能是"晴天"），请自行纠正为最可能的歌曲名。

**再次强调：如果用户这一轮没有说任何关于"听歌/放音乐/播放"的话，就不要输出任何音乐标签！**

【回复规则】
1. 闲聊/问答/确认状态 → 直接回复，**不要**输出 [ACTIONS] 标签。对于"你喜欢音乐吗""这首歌好听吗"等非指令性句子，只做文字回复。
2. 用户明确要求播放音乐 → 用 [ACTIONS] 标签标记操作
3. 用户明确要求控制设备 → 用 [ACTIONS]{{"devices":[{{"device":"bedroom_light","action":"on"}}]}}[/ACTIONS]
4. 用户要求写代码 → 回复"正在生成代码，请说「允许」开始执行"（不要输出技术细节）
5. 不要虚构实时信息（天气、新闻等），不确定就诚实说明
6. 需要联网搜索时用 [SEARCH]搜索关键词[/SEARCH] 标签

【版权歌曲】
系统已接入网易云音乐，**可以播放绝大多数中文流行歌曲**（周杰伦、林俊杰、邓紫棋等华语歌手均可）。
- 用户指定歌名/歌手 → 正常输出音乐标签，后端会自动搜索并获取可播放链接
- 只有极少数 VIP 专属/数字专辑歌曲可能无法播放，后端会自动降级告知用户
- **不要**再主动告知用户"无法播放版权歌曲"

【歌单播放】
当用户请求播放某类情绪/场景/风格歌曲时：
- **必须**从【当前可用歌单】列表中**原样复制**一个歌单名到 playlist 字段
- **绝对禁止**自行创作、翻译、改写歌单名
- 参考映射：
  * 学习/专注/看书/备考/焦虑/紧张 → 选包含"轻音乐""专注""学习"关键词的歌单
  * 助眠/睡觉/失眠/休息/冥想/放松 → 选包含"轻音乐""助眠""放松"关键词的歌单
  * 日常/随便放/来点音乐/放首歌（无具体歌名）→ 优先选"收藏"，其次第一个歌单
- 输出格式：[ACTIONS]{{\"music\":{{\"action\":\"play\",\"playlist\":\"<精确歌单名>\"}}}}[/ACTIONS]
- **不确定时**：宁可空着 playlist 让后端自己选，也**绝不编造**
- 用户也可以直接指定歌名/歌手名，后端会通过网易云搜索并播放（无需 playlist）

【CET-6 备考功能 — 非常重要】
当用户想备考大学英语六级、做真题、练习听力时，你可以在回复中输出隐藏指令标签（系统会自动执行，用户看不到标签本身）：

可用操作和输出格式：
- 随机来一套真题 → [ACTIONS]{{"cet6":{{"action":"random_paper"}}}}[/ACTIONS]
  如果用户指定了年份 → [ACTIONS]{{"cet6":{{"action":"random_paper","year":2025,"month":6}}}}[/ACTIONS]
  **重要**：把用户说的年份原样填入 year，不要改成你以为"存在"的年份！
- 用户精确指定年份/月份/套号 → [ACTIONS]{{"cet6":{{"action":"paper","year":2021,"month":6,"set":1}}}}[/ACTIONS]
  例：用户说"做2021年6月的第一套" → year=2021, month=6, set=1
- 浏览本地题库有哪些试卷 → [ACTIONS]{{"cet6":{{"action":"browse"}}}}[/ACTIONS]
- 联网搜索历年真题 → [ACTIONS]{{"cet6":{{"action":"search","year":2021}}}}[/ACTIONS]
  如果用户没指定年份，year 可省略
- 查看当前试卷答案 → [ACTIONS]{{"cet6":{{"action":"answers"}}}}[/ACTIONS]
- 播放当前试卷的听力音频 → [ACTIONS]{{"cet6":{{"action":"listening"}}}}[/ACTIONS]

{CET6_PAPER_SUMMARY}

**判断规则**：
- 用户表达了"做真题""练习""来一套""刷题""模拟考试"等明确要做题的关键词 → **必须**输出对应标签，年份严格按用户说的填
- 用户只是问"怎么备考""有什么建议""如何学英语""背单词"→ 只做文字回复，**不要**输出标签
- 用户说"播放六级听力""听听六级听力""放听力"→ 输出 listening 标签（**不是**音乐标签！）
- 用户说"对答案""看看答案""核对答案"→ 输出 answers 标签
- 用户说"有没有2021年的真题""找找去年的"→ 输出 search 标签

**重要提示**：
- 你依然要输出自然的文字回复（如"好，给你找了一套2025年6月的真题，先做听力部分吧😊"）
- 标签和文字回复可以同时存在，标签会被系统自动过滤，用户只会看到你的文字
- 试卷 PDF 和听力音频会由系统自动推送给用户，你不需要在文字中描述文件内容或格式
- 你的文字回复要有人情味，像一个热心的学长/学姐在帮忙备考

【学习/工作计划参考】
当用户要求制定学习或工作计划时，参考以下框架（但**不必严格按此结构**，每次回复要有变化）：

核心原则：学习计划的核心是"专注攻克弱点"，不是"放松"。

时间拆分思路（基准 1 小时）：
· 前 ~35%：攻克最难的一题/一段/一个概念 —— 不追求完美，先把硬骨头啃下来
· 中间 ~50%：做题/实践 + 错题回看 —— 检验理解，标记卡住的地方
· 最后 ~15%：把错因写成一句话备忘 —— 最容易被跳过但最有长期价值

**重要：不要照搬模板！** 每次回复的开头、结构、语气都要变化。
像朋友/学长在帮忙，而不是机器人在念模板。"""


# ACTIONS 正则（同时匹配方括号 [ 和中文书名号 【 ）
ACTIONS_RE = re.compile(r'[\[【]ACTIONS[]】](.*?)[\[【]/ACTIONS[]】]', re.DOTALL)
ACTIONS_STRIP_RE = re.compile(r'[\[【]ACTIONS[]】].*?[\[【]/ACTIONS[]】]', re.DOTALL)
SEARCH_TAG_RE = re.compile(r'\[SEARCH\].*?\[/SEARCH\]', re.DOTALL)


def _strip_actions_tags(text: str) -> str:
    """防御性清除所有 [ACTIONS]...[/ACTIONS] 和 【ACTIONS】...【/ACTIONS】 标签。
    作为流式过滤之外的最终防线，确保 ACTIONS 永远不会泄露到用户可见文本中。"""
    if not text:
        return text
    return ACTIONS_STRIP_RE.sub('', text).strip()


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
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """Ollama 流式调用

    Args:
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签内容会追加到此列表。
                     调用方可在流结束后读取并执行音乐/设备操作。
        cancel_event: 若设置，当 event.is_set() 时中断流式生成（用于唤醒词打断）。
    """
    try:
        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": 0.85, "top_p": 0.92},
        )
        buffer = ""
        in_tag = False
        in_search = False
        open_marker = ""   # 记录当前 ACTIONS 开始标记，用于配对闭合标签

        for chunk in stream:
            # 唤醒词打断检测：前端发送 cancel → cancel_event 被设置 → 中断生成
            if cancel_event and cancel_event.is_set():
                logger.info("Ollama 流被 cancel_event 中断（唤醒词打断）")
                break
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
                        # 修复：不再 continue，fall through 让可见文字立即输出
                    else:
                        continue  # 仍在 ACTIONS 标签内，跳过正常输出

                # 正常流式输出（含防御性 ACTIONS 清除）
                if buffer and not in_tag and not in_search:
                    clean = _strip_actions_tags(buffer)
                    if clean:
                        yield clean
                    buffer = ""
        if buffer and not in_tag and not in_search:
            clean = _strip_actions_tags(buffer)
            if clean:
                yield clean
        if buffer and in_tag and open_marker:
            # 未闭合的 ACTIONS 标签：保存 JSON 用于执行
            if actions_out is not None:
                actions_out.append(buffer)
            # 同时提取可见文本（去除 [ACTIONS]... 残片后的内容）
            unclosed_re = re.compile(r'[\[【]ACTIONS[]】].*', re.DOTALL)
            visible = unclosed_re.sub('', buffer).strip()
            if visible:
                yield visible

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
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """DeepSeek 流式调用，过滤 [SEARCH] / [ACTIONS] 标签

    Args:
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签内容会追加到此列表。
                     调用方可在流结束后读取并执行音乐/设备操作。
        cancel_event: 若设置，当 event.is_set() 时中断流式生成（用于唤醒词打断）。
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
        # 唤醒词打断检测
        if cancel_event and cancel_event.is_set():
            logger.info("DeepSeek 流被 cancel_event 中断（唤醒词打断）")
            break
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
                # 修复：不再 continue，fall through 让可见文字立即输出
            else:
                continue  # 仍在 ACTIONS 标签内，跳过正常输出

        # 正常流式输出（含防御性 ACTIONS 清除）
        if buffer and not in_tag and not in_search:
            clean = _strip_actions_tags(buffer)
            if clean:
                yield clean
            buffer = ""

    # 流结束后 flush 剩余缓冲区（最终防线清除）
    if buffer and not in_tag and not in_search:
        clean = _strip_actions_tags(buffer)
        if clean:
            yield clean
    # 若流结束时仍在 ACTIONS 标签内（模型异常截断），保存 JSON 并输出可见文本
    if buffer and in_tag and open_marker:
        if actions_out is not None:
            actions_out.append(buffer)
        unclosed_re = re.compile(r'[\[【]ACTIONS[]】].*', re.DOTALL)
        visible = unclosed_re.sub('', buffer).strip()
        if visible:
            yield visible


# ===== 双引擎调度入口 =====

async def generate_stream(
    prompt: str,
    model: str = DEFAULT_LOCAL_MODEL,
    memory_context: str = "",
    conversation_history: list[dict] | None = None,
    prefer_cloud: bool = False,           # 调用方要求优先云端
    model_used: list[str] | None = None,  # 传出参数：实际使用的模型名
    actions_out: list[str] | None = None, # 侧通道：收集流式中的 [ACTIONS] 标签
    cancel_event: asyncio.Event | None = None,  # 唤醒词打断信号
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
        cancel_event: 若设置，当 event.is_set() 时中断流式生成（用于唤醒词打断）
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
                cancel_event=cancel_event,
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
        async for token in _ollama_stream(messages, model, actions_out=actions_out, cancel_event=cancel_event):
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
