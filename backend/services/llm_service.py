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

# 可用设备列表（注入提示词）
DEVICE_LIST = """- 灯: 卧室灯、客厅灯、厨房灯、卫生间灯、书房灯 (打开/关闭)
- 窗帘: 客厅窗帘 (拉开/关上)
- 空调: 可调温度 (打开/关闭/调高/调低)
- 热水器: (打开/关闭)"""

SYSTEM_PROMPT = f"""你是"小智"，一个温暖的智能语音助手。像朋友一样口语化聊天，亲切自然。
回复长度随问题调整：简单的一两句话，复杂的多说几句（一般50-200字）。适度用emoji🙂和Markdown。

【可用设备】
{DEVICE_LIST}

【操作标签】附加在回复末尾，系统自动过滤：
· 音乐：[ACTIONS]{{"music":{{"action":"play","query":"歌名或歌手"}}}}[/ACTIONS]
  支持 next(切歌) pause(暂停) prev(上一首) resume(继续)，泛化请求 query 留空 ""
· 设备：[ACTIONS]{{"devices":[{{"device":"bedroom_light","action":"on"}}]}}[/ACTIONS]
· CET6：[ACTIONS]{{"cet6":{{"action":"random_paper"}}}}[/ACTIONS]
  可选: random_paper(随机真题) paper(指定套题) browse(浏览) search(搜索) answers(答案) listening(听力)
  指定年份加 "year""month"，精确套题再加 "set"
· 搜索：[SEARCH]关键词[/SEARCH]

⚠️ 仅用户明确说听歌/放音乐时才输出音乐标签，闲聊绝对不要输出。
⚠️ 歌单名从【当前可用歌单】原样复制，禁止编造。已接入网易云音乐。

{CET6_PAPER_SUMMARY}

写代码请求→回复"正在生成，请说「允许」开始"。"""


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
            options={"temperature": 0.8, "top_p": 0.9},
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
    open_marker = ""

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

        # 正常流式输出（含防御性 ACTIONS 清除）
        if buffer and not in_tag and not in_search:
            clean = _strip_actions_tags(buffer)
            if clean:
                yield clean
            buffer = ""

    # Flush 剩余 buffer
    if buffer and not in_tag and not in_search:
        clean = _strip_actions_tags(buffer)
        if clean:
            yield clean
    if buffer and in_tag and open_marker:
        if actions_out is not None:
            actions_out.append(buffer)
        unclosed_re = re.compile(r'[\[【]ACTIONS[]】].*', re.DOTALL)
        visible = unclosed_re.sub('', buffer).strip()
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
