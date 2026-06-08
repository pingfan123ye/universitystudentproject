"""
大模型调用服务 —— 双引擎调度器
封装 Ollama（本地）和 DeepSeek（云端），根据配置自动切换。
"""
import asyncio
import json
import logging
import re
import threading
from typing import AsyncIterator

import ollama

from services.engine_config import get_config
from services import deepseek_provider
from services.cet6_service import _load_index as _cet6_load_index

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "qwen3:8b"


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

# 可用设备列表（注入提示词，含英文 ID 映射，LLM 必须使用英文 ID）
DEVICE_LIST = """- bedroom_light=卧室灯 (on/off)
- living_light=客厅灯 (on/off)
- kitchen_light=厨房灯 (on/off)
- bathroom_light=卫生间灯 (on/off)
- study_light=书房灯 (on/off)
- living_curtain=客厅窗帘 (open/close)
- ac=空调 (on/off)
- water_heater=热水器 (on/off)"""

SYSTEM_PROMPT = f"""你是"小智"，温暖的智能语音助手。

【风格】口语化聊天，像朋友一样自然。回复长度随问题调整：问候1-2句，复杂问题50-200字。适度用emoji🙂。不用星号动作描述。对用户情绪共情。

【情景猜测】可以根据上下文推测用户意图，但要紧贴用户说的话，不要发散：
- ✅ "焦虑，放点不吵的歌" → 共情安抚 + 播放轻音乐（只做这一个操作）
- ✅ "好热" → 问一句"要不要开空调？"（不要直接开）
- ✅ "好黑" → 问一句"帮你开灯吗？"（不要直接开）
- ❌ 用户说焦虑想听歌 → 不要顺便建议开灯、做CET6真题（完全无关）
- ❌ 用户说累了 → 不要输出任何操作标签，只文字共情
- ❌ 用户只说了一个场景（如"学习"）→ 不要自作主张开设备 + 放歌 + 做题全来一遍，选最贴合的一项

【设备】{DEVICE_LIST}
设备操作必须用英文 device ID，使用 on/off/open/close：
[ACTIONS]{{"devices":[{{"device":"study_light","action":"on"}}]}}[/ACTIONS]

【音乐】用户说想听歌时：
[ACTIONS]{{"music":{{"action":"play","query":"轻音乐"}}}}[/ACTIONS]
action: play/pause/next/prev/resume。版权歌→推荐本地歌单替代。

【CET6】用户明确要做题时：
[ACTIONS]{{"cet6":{{"action":"random_paper"}}}}[/ACTIONS]

{CET6_PAPER_SUMMARY}
写代码请求→回复"正在生成，请说「允许」开始"。禁止自创标签名。"""


# ACTIONS 正则（同时匹配方括号 [ 和中文书名号 【 ）
ACTIONS_RE = re.compile(r'[\[【]ACTIONS[]】](.*?)[\[【]/ACTIONS[]】]', re.DOTALL)
ACTIONS_STRIP_RE = re.compile(r'[\[【]ACTIONS[]】].*?[\[【]/ACTIONS[]】]', re.DOTALL)
SEARCH_TAG_RE = re.compile(r'\[SEARCH\].*?\[/SEARCH\]', re.DOTALL)
# 防御性剥离：LLM 可能自创的非标准标签名（如 [音乐]/[CET6]/[设备]）
NONSTANDARD_TAG_RE = re.compile(r'\[(?:音乐|CET6|cet6|设备|装置)\].*?\[/(?:音乐|CET6|cet6|设备|装置)\]', re.DOTALL)
NONSTANDARD_UNCLOSED_RE = re.compile(r'\[(?:音乐|CET6|cet6|设备|装置)\]', re.DOTALL)


def _strip_actions_tags(text: str) -> str:
    """防御性清除所有 ACTIONS/非标准标签。作为流式过滤之外的最终防线。"""
    if not text:
        return text
    text = ACTIONS_STRIP_RE.sub('', text)
    text = NONSTANDARD_TAG_RE.sub('', text)
    # 清除无闭合的非标准标签开标签（从 [音乐/ [CET6/ [设备] 到行尾或文末）
    text = NONSTANDARD_UNCLOSED_RE.sub('', text)
    return text.strip()


def strip_search_tags(text: str) -> str:
    """去除回复中的 [SEARCH]...[/SEARCH] 标签"""
    return SEARCH_TAG_RE.sub('', text).strip()


def _try_fix_json(raw: str) -> str | None:
    """尝试修复 LLM 常见的 JSON 格式错误，修复成功返回修复后字符串，否则返回 None"""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    fixes_tried = []
    # 0. 双花括号 {{ → { （LLM 从旧版模板学到错误格式，同时修复不平衡的 }} → }）
    if '{{' in s or '}}' in s:
        fixed = s.replace('{{', '{').replace('}}', '}')
        fixes_tried.append(fixed)
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


# ===== Ollama 本地提供者（线程池异步，避免阻塞事件循环） =====

import queue as _queue_mod


def _ollama_chat_sync(
    messages: list[dict],
    model: str,
    token_queue: "_queue_mod.Queue",
    cancel_event: asyncio.Event | None,
    stop_event: threading.Event,
):
    """在后台线程运行 ollama.chat()，token 通过队列传出"""
    try:
        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={
                "temperature": 0.8,
                "top_p": 0.9,
                "num_predict": 512,       # 限制最大输出 token，避免长回复拖慢
                "num_ctx": 2048,           # 上下文窗口（平衡速度与记忆）
                "repeat_penalty": 1.05,    # 减少重复生成浪费 token
            },
            keep_alive="5m",              # 保持模型在内存 5 分钟，减少冷启动
        )
        for chunk in stream:
            if stop_event.is_set():
                break
            if cancel_event and cancel_event.is_set():
                break
            if chunk and "message" in chunk and "content" in chunk["message"]:
                token_queue.put(("token", chunk["message"]["content"]))
        token_queue.put(("done", None))
    except Exception as e:
        token_queue.put(("error", str(e)))


async def _ollama_token_iter(
    messages: list[dict],
    model: str,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """Async generator: 在线程池中运行 ollama.chat，事件循环永不阻塞"""
    q: "_queue_mod.Queue" = _queue_mod.Queue()
    stop = threading.Event()
    t = threading.Thread(
        target=_ollama_chat_sync,
        args=(messages, model, q, cancel_event, stop),
        daemon=True,
    )
    t.start()

    loop = asyncio.get_event_loop()
    try:
        while True:
            type_, data = await loop.run_in_executor(None, q.get)
            if type_ == "token":
                yield data
            elif type_ == "done":
                break
            elif type_ == "error":
                raise RuntimeError(f"Ollama 服务异常: {data}")
    finally:
        stop.set()


async def _ollama_stream(
    messages: list[dict],
    model: str = DEFAULT_LOCAL_MODEL,
    actions_out: list[str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """Ollama 流式调用（线程池异步，支持首 token 超时）

    Args:
        actions_out: 若提供，流式过程中收集到的 [ACTIONS] 标签内容会追加到此列表。
        cancel_event: 若设置，当 event.is_set() 时中断流式生成（用于唤醒词打断）。
    """
    buffer = ""
    in_tag = False
    in_search = False
    in_nonstd = False
    nonstd_close = ""
    open_marker = ""

    NONSTD_OPEN = ["[音乐]", "[CET6]", "[cet6]", "[设备]"]
    NONSTD_CLOSE_MAP = {"[音乐]": "[/音乐]", "[CET6]": "[/CET6]", "[cet6]": "[/cet6]", "[设备]": "[/设备]"}

    async for token in _ollama_token_iter(messages, model, cancel_event):
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

        # 非标准标签检测（[音乐]/[CET6]/[设备]）：LLM 幻觉标签，直接丢弃
        if not in_nonstd and not in_tag:
            for opener in NONSTD_OPEN:
                if opener in buffer:
                    tag_start = buffer.find(opener)
                    before = buffer[:tag_start]
                    if before:
                        clean_before = _strip_actions_tags(before)
                        if clean_before:
                            yield clean_before
                    buffer = buffer[tag_start:]
                    in_nonstd = True
                    nonstd_close = NONSTD_CLOSE_MAP[opener]
                    break
        if in_nonstd:
            if nonstd_close in buffer:
                tag_end = buffer.find(nonstd_close) + len(nonstd_close)
                logger.info(f"流式过滤: 丢弃非标准标签 {buffer[:tag_end][:80]}")
                buffer = buffer[tag_end:]
                in_nonstd = False
                nonstd_close = ""
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
                if actions_out is not None:
                    actions_out.append(buffer[:tag_end])
                buffer = buffer[tag_end:]
                in_tag = False
                open_marker = ""
            else:
                continue

        # 正常流式输出（含防御性清除）
        if buffer and not in_tag and not in_search and not in_nonstd:
            clean = _strip_actions_tags(buffer)
            if clean:
                yield clean
            buffer = ""

    # Flush 剩余 buffer
    if buffer and not in_tag and not in_search and not in_nonstd:
        clean = _strip_actions_tags(buffer)
        if clean:
            yield clean
    if buffer and (in_tag or in_nonstd) and open_marker:
        if in_tag and actions_out is not None:
            actions_out.append(buffer)
        unclosed_re = re.compile(r'[\[【]ACTIONS[]】].*', re.DOTALL)
        visible = unclosed_re.sub('', buffer).strip()
        visible = NONSTANDARD_UNCLOSED_RE.sub('', visible).strip()
        if visible:
            yield visible


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
    in_nonstd = False
    nonstd_close = ""
    open_marker = ""   # 记录当前 ACTIONS 开始标记，用于配对闭合标签

    NONSTD_OPEN = ["[音乐]", "[CET6]", "[cet6]", "[设备]"]
    NONSTD_CLOSE_MAP = {"[音乐]": "[/音乐]", "[CET6]": "[/CET6]", "[cet6]": "[/cet6]", "[设备]": "[/设备]"}

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

        # 非标准标签检测（[音乐]/[CET6]/[设备]）：LLM 幻觉标签，直接丢弃
        if not in_nonstd and not in_tag:
            for opener in NONSTD_OPEN:
                if opener in buffer:
                    tag_start = buffer.find(opener)
                    before = buffer[:tag_start]
                    if before:
                        clean_before = _strip_actions_tags(before)
                        if clean_before:
                            yield clean_before
                    buffer = buffer[tag_start:]
                    in_nonstd = True
                    nonstd_close = NONSTD_CLOSE_MAP[opener]
                    break
        if in_nonstd:
            if nonstd_close in buffer:
                tag_end = buffer.find(nonstd_close) + len(nonstd_close)
                logger.info(f"DeepSeek 流式过滤: 丢弃非标准标签 {buffer[:tag_end][:80]}")
                buffer = buffer[tag_end:]
                in_nonstd = False
                nonstd_close = ""
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
        if buffer and not in_tag and not in_search and not in_nonstd:
            clean = _strip_actions_tags(buffer)
            if clean:
                yield clean
            buffer = ""

    # 流结束后 flush 剩余缓冲区（最终防线清除）
    if buffer and not in_tag and not in_search and not in_nonstd:
        clean = _strip_actions_tags(buffer)
        if clean:
            yield clean
    # 若流结束时仍在标签内（模型异常截断），保存 JSON 并输出可见文本
    if buffer and (in_tag or in_nonstd) and open_marker:
        if in_tag and actions_out is not None:
            actions_out.append(buffer)
        unclosed_re = re.compile(r'[\[【]ACTIONS[]】].*', re.DOTALL)
        visible = unclosed_re.sub('', buffer).strip()
        visible = NONSTANDARD_UNCLOSED_RE.sub('', visible).strip()
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

    # ── 本地路径（首 token 超时自动切云端）──
    logger.info(f"双引擎 → 本地 (model={model})")
    if model_used is not None:
        model_used.append(f"ollama:{model}")

    _timeout = config.get("timeout_seconds", 8)
    _cloud_available = deepseek_provider.is_available()

    def _fallback_to_cloud():
        """超时/失败时切云端的内联函数"""
        if model_used is not None and len(model_used) > 0:
            model_used[-1] = f"deepseek:{config.get('cloud_model')}"
        if actions_out is not None:
            actions_out.clear()

    try:
        agen = _ollama_stream(messages, model, actions_out=actions_out, cancel_event=cancel_event)
        # 等待首 token，超时则切云端
        first_token = await asyncio.wait_for(agen.__anext__(), timeout=_timeout)
        yield first_token
        async for token in agen:
            yield token
    except StopAsyncIteration:
        # 本地模型无输出（极罕见）
        if config.get("default_mode") == "local_only":
            raise RuntimeError("本地模型无输出（local_only 模式，不切云端）")
        if _cloud_available:
            logger.info("本地模型无输出，切云端")
            _fallback_to_cloud()
            async for token in _deepseek_stream(
                messages=messages,
                model=config.get("cloud_model"),
                enable_search=enable_search,
                actions_out=actions_out,
            ):
                yield token
    except asyncio.TimeoutError:
        if config.get("default_mode") == "local_only":
            raise RuntimeError(
                f"本地 LLM 首 token 超时 ({_timeout}s)，"
                f"当前为 local_only 模式不切云端。"
                f"建议：减小模型尺寸或缩短提示词"
            )
        logger.warning(f"本地 LLM 首 token 超时 ({_timeout}s)，切云端")
        if cancel_event:
            cancel_event.set()
        if _cloud_available:
            _fallback_to_cloud()
            async for token in _deepseek_stream(
                messages=messages,
                model=config.get("cloud_model"),
                enable_search=enable_search,
                actions_out=actions_out,
            ):
                yield token
        else:
            raise RuntimeError(f"本地 LLM 超时 ({_timeout}s) 且云端不可用")
    except RuntimeError as e:
        # Ollama 错误
        if config.get("default_mode") == "local_only":
            raise
        if _cloud_available:
            logger.warning(f"Ollama 失败，切到云端: {e}")
            _fallback_to_cloud()
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
