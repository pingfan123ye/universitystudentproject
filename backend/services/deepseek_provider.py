"""
DeepSeek API 提供者 —— 支持流式输出 + 联网搜索 (enable_search)
使用 OpenAI 兼容接口格式。
"""
import json
import logging
import os
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"


def _resolve_api_key() -> str | None:
    """从环境变量或项目配置读取 DeepSeek API Key"""
    # 1. 环境变量
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key
    # 2. 项目配置文件
    try:
        cfg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", ".reasonix", "config.json",
        )
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
                return cfg.get("apiKey") or cfg.get("api_key") or cfg.get("DEEPSEEK_API_KEY")
    except Exception:
        pass
    return None


def is_available() -> bool:
    """检查 DeepSeek API Key 是否已配置"""
    return _resolve_api_key() is not None


async def generate_stream(
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    enable_search: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> AsyncIterator[str]:
    """
    流式调用 DeepSeek API。

    Args:
        messages: OpenAI 格式消息列表 [{"role":"system"/"user"/"assistant", "content":str}, ...]
        model: 模型名
        enable_search: 是否启用联网搜索
        temperature: 温度
        max_tokens: 最大生成长度
    """
    api_key = _resolve_api_key()
    if not api_key:
        yield "【DeepSeek API Key 未配置，请设置环境变量 DEEPSEEK_API_KEY】\n"
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if enable_search:
        payload["enable_search"] = True
        logger.info("DeepSeek 联网搜索已启用")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    yield f"\n【DeepSeek API 错误 {response.status_code}: {error_body.decode('utf-8', errors='replace')[:200]}】\n"
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue

    except httpx.TimeoutException:
        yield "\n【DeepSeek API 请求超时】\n"
    except Exception as e:
        yield f"\n【DeepSeek API 异常: {str(e)}】\n"


async def check_available(model: str = DEFAULT_MODEL) -> bool:
    """检查 DeepSeek API 是否可用"""
    api_key = _resolve_api_key()
    if not api_key:
        return False
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{DEEPSEEK_BASE_URL}/models",
                headers=headers,
            )
            return resp.status_code == 200
    except Exception:
        return False
