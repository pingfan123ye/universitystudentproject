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
1. 闲聊/问答/确认状态 → 直接回复即可，**不要**输出任何 [ACTIONS] 标签。对于包含"音乐""歌"但非指令性的句子（如"你喜欢音乐吗""这首歌好听吗"），只做文字回复，绝不输出 [ACTIONS]。
2. 用户明确要求播放音乐 → 用 [ACTIONS]{{"music":{{"action":"play","query":"歌曲名 歌手"}}}}[/ACTIONS] 标记
   - 用户说"播放夜空中最亮的星" → query 填 "夜空中最亮的星"
   - 用户说"来首歌" / "放点音乐" 但没指定歌名 → query 留空 ""
   - **绝不要**在闲聊回复中因为提到"听歌"两个字就加 [ACTIONS]
3. 用户明确要求控制设备 → 用 [ACTIONS]{{"devices":[{{"device":"bedroom_light","action":"on"}}]}}[/ACTIONS] 标记
4. 暂停/下一首/上一首 → [ACTIONS]{{"music":{{"action":"pause"}}}}[/ACTIONS] 等
5. 用户要求写代码 → 说明"正在调用Reasonix生成代码，说「允许」开始执行"
6. 回复简洁自然，一般不超过 80 字
7. 不要虚构实时信息（天气、新闻等），不确定就诚实说明
8. 如果你需要联网获取实时信息来回答，可以用 [SEARCH]搜索关键词[/SEARCH] 标记，系统会自动搜索并将结果替换到回复中"""

# 保留 ACTIONS 正则用于未来扩展
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
        in_search = False

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

                # [ACTIONS] 标签检测：在标签内不输出
                if not in_tag and "[ACTIONS]" in buffer:
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

    # 本地调用（直接流式输出，异常时切云端）
    try:
        async for token in _ollama_stream(messages, model):
            yield token
    except RuntimeError as e:
        # Ollama 错误，检查是否可切云端
        if deepseek_provider.is_available():
            logger.warning(f"Ollama 失败，切到云端: {e}")
            if model_used is not None and len(model_used) > 0:
                model_used[-1] = f"deepseek:{config.get('cloud_model')}"
            async for token in _deepseek_stream(
                messages=messages,
                model=config.get("cloud_model"),
                enable_search=enable_search,
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
