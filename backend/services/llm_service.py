"""
大模型调用服务 —— 封装 Ollama API，支持流式输出 + 反向调用小爱/Reasonix
"""
import json
import re
from typing import AsyncIterator

import ollama

DEFAULT_MODEL = "qwen2.5:7b"

# 可用设备列表（注入提示词）
DEVICE_LIST = """- 灯: 卧室灯、客厅灯、厨房灯、卫生间灯、书房灯 (打开/关闭)
- 窗帘: 客厅窗帘 (拉开/关上)
- 空调: 可调温度 (打开/关闭/调高/调低)
- 热水器: (打开/关闭)"""

SYSTEM_PROMPT = f"""你是"小智"，一个智能语音助手。你可以协同「小爱」和「Reasonix」一起为用户服务。

你有三个协同伙伴：
1. **你自己（大模型）**：负责思考、推理、聊天、回答问题
2. **小爱**：负责执行设备操作和播放音乐（你能反向调用小爱）
3. **Reasonix**：负责编程、写代码、执行命令行操作（通过「工作助手」「克劳德」「贾维斯」触发）

当前可用的虚拟设备：
{DEVICE_LIST}

小爱能做的事情：
- 控制上述设备（你说"打开卧室灯"，小爱去执行）
- 播放音乐、暂停、下一首、音量调节

回复规则（非常重要）：
1. 如果用户要求设备操作或播放音乐 → 在回复中说明"正在调用小爱[做什么]"
   示例："好的，正在调用小爱帮你打开客厅灯并播放音乐，希望能帮你放松"
2. 如果用户要求写代码 → 说明"正在调用Reasonix生成代码，说「允许」开始执行"
3. 如果只是闲聊或问答 → 直接回复即可
4. 回复简洁自然，一般不超过 100 字
5. 不要虚构实时信息（天气、新闻等），不确定就诚实说明
6. 绝对不要在回复中添加 JSON、XML 或任何格式标签"""

# 用于提取回复中 ACTIONS 块的正则
ACTIONS_RE = re.compile(r'\[ACTIONS\](.*?)\[/ACTIONS\]', re.DOTALL)


def parse_actions(text: str) -> tuple[str, dict | None]:
    """从文本中提取 [ACTIONS] JSON，返回 (纯净文本, actions_dict)"""
    m = ACTIONS_RE.search(text)
    if m:
        clean = ACTIONS_RE.sub('', text).strip()
        try:
            actions = json.loads(m.group(1))
            return clean, actions
        except json.JSONDecodeError:
            return text, None
    return text, None


async def generate_stream(prompt: str, model: str = DEFAULT_MODEL, memory_context: str = "",
                          conversation_history: list[dict] | None = None) -> AsyncIterator[str]:
    """流式调用 Ollama，实时过滤 ACTIONS 标签。

    Args:
        prompt: 当前用户输入
        model: Ollama 模型名
        memory_context: 注入的长期记忆
        conversation_history: 对话历史 [{"role":"user"/"assistant", "content":str}, ...]
    """
    system_msg = SYSTEM_PROMPT
    if memory_context:
        system_msg += "\n" + memory_context

    # 构建 messages（含对话历史）
    messages = [{"role": "system", "content": system_msg}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

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
                yield clean

    except ollama.ResponseError as e:
        raise RuntimeError(f"Ollama 模型调用失败: {e.error}") from e
    except Exception as e:
        raise RuntimeError(f"大模型服务异常: {str(e)}") from e


async def check_model_available(model: str = DEFAULT_MODEL) -> bool:
    """检查指定模型是否已在 Ollama 中可用"""
    try:
        models = ollama.list()
        model_names = [m.get("name", "") for m in models.get("models", [])]
        return any(model in name or name in model for name in model_names)
    except Exception:
        return False
