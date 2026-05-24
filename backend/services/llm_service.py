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

回复规则（非常重要）：
1. 如果用户要求设备操作或播放音乐 → 在回复中说明"正在执行[做什么]"
2. 如果用户要求写代码 → 说明"正在调用Reasonix生成代码，说「允许」开始执行"
3. 如果只是闲聊或问答 → 直接回复即可
4. 回复简洁自然，一般不超过 100 字
5. 不要虚构实时信息（天气、新闻等），不确定就诚实说明
6. 如果你需要联网获取实时信息来回答，可以用 [SEARCH]搜索关键词[/SEARCH] 标记，系统会自动搜索并将结果替换到回复中
7. 绝对不要在回复中添加 JSON、XML 或任何格式标签"""

ACTIONS_RE = re.compile(r'\[ACTIONS\](.*?)\[/ACTIONS\]', re.DOTALL)
SEARCH_TAG_RE = re.compile(r'\[SEARCH\].*?\[/SEARCH\]', re.DOTALL)


def strip_search_tags(text: str) -> str:
    """去除回复中的 [SEARCH]...[/SEARCH] 标签"""
    return SEARCH_TAG_RE.sub('', text).strip()


def parse_actions(text: str) -> tuple[str, dict | None]:
    m = ACTIONS_RE.search(text)
    if m:
        clean = ACTIONS_RE.sub('', text).strip()
        try:
            actions = json.loads(m.group(1))
            return clean, actions
        except json.JSONDecodeError:
            return text, None
    return text, None


# ===== Ollama 本地提供者 =====

async def _ollama_stream(
    messages: list[dict],
    model: str = DEFAULT_LOCAL_MODEL,
) -> AsyncIterator[str]:
    """Ollama 流式调用"""
    try:
        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": 0.7, "top_p": 0.9},
        )
        buffer = ""
        in_tag = False

        for chunk in stream:
            if chunk and "message" in chunk and "content" in chunk["message"]:
                token = chunk["message"]["content"]
                if not token:
                    continue
                buffer += token
                if "[ACTIONS]" in buffer and not in_tag:
                    tag_start = buffer.find("[ACTIONS]")
                    before = buffer[:tag_start]
                    if before:
                        yield before
                    buffer = buffer[tag_start:]
                    in_tag = True
                if in_tag:
                    if "[/ACTIONS]" in buffer:
                        tag_end = buffer.find("[/ACTIONS]") + len("[/ACTIONS]")
                        buffer = buffer[tag_end:]
                        in_tag = False
                    continue
                if not in_tag and buffer:
                    yield buffer
                    buffer = ""

        if buffer:
            clean, _ = parse_actions(buffer)
            if clean:
                yield strip_search_tags(clean)
            else:
                stripped = strip_search_tags(buffer)
                if stripped:
                    yield stripped

    except ollama.ResponseError as e:
        raise RuntimeError(f"Ollama 模型调用失败: {e.error}") from e
    except Exception as e:
        raise RuntimeError(f"Ollama 服务异常: {str(e)}") from e


# ===== DeepSeek 云端提供者 =====

async def _deepseek_stream(
    messages: list[dict],
    model: str = "deepseek-v4-flash",
    enable_search: bool = False,
) -> AsyncIterator[str]:
    """DeepSeek 流式调用"""
    async for token in deepseek_provider.generate_stream(
        messages=messages,
        model=model,
        enable_search=enable_search,
    ):
        yield token


# ===== 双引擎调度入口 =====

async def generate_stream(
    prompt: str,
    model: str = DEFAULT_LOCAL_MODEL,
    memory_context: str = "",
    conversation_history: list[dict] | None = None,
    prefer_cloud: bool = False,           # 调用方要求优先云端
    model_used: list[str] | None = None,  # 传出参数：实际使用的模型名
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

    # 本地调用（带超时，超时则切云端）
    try:
        if config.get("default_mode") == "local_first":
            timeout = config.get("timeout_seconds", 8)
            local_task = asyncio.create_task(
                _collect_async_gen(_ollama_stream(messages, model))
            )
            done, pending = await asyncio.wait(
                [local_task], timeout=timeout,
            )
            if local_task in done:
                for token in local_task.result():
                    yield token
                return
            # 超时 → 取消本地，切云端
            local_task.cancel()
            logger.warning(f"本地模型超时({timeout}s)，切换到云端")
            if cloud_available:
                if model_used is not None:
                    model_used[-1] = f"deepseek:{cloud_model}"
                async for token in _deepseek_stream(
                    messages=messages,
                    model=cloud_model,
                    enable_search=enable_search,
                ):
                    yield token
                return
            else:
                yield "\n【本地模型超时，云端也未配置，请检查模型状态】\n"
                return
        else:
            async for token in _ollama_stream(messages, model):
                yield token
    except RuntimeError as e:
        # Ollama 错误，检查是否可切云端
        if deepseek_provider.is_available():
            logger.warning(f"Ollama 失败，切到云端: {e}")
            async for token in _deepseek_stream(
                messages=messages,
                model=config.get("cloud_model"),
                enable_search=enable_search,
            ):
                yield token
        else:
            raise


async def _collect_async_gen(gen) -> list[str]:
    """将异步生成器收集为列表（用于超时控制）"""
    tokens = []
    async for t in gen:
        tokens.append(t)
    return tokens


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
