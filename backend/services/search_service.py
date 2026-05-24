"""
联网搜索服务 —— 为 LLM 提供实时信息检索
当前使用 DuckDuckGo（免费，无需 API Key），可扩展 SearXNG。
"""
import asyncio
import logging
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 8  # 搜索超时（秒）
MAX_RESULTS = 5     # 返回最多几条结果


async def search_duckduckgo(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    通过 DuckDuckGo Lite API 搜索。

    Returns: [{"title": str, "snippet": str, "url": str}, ...]
    """
    url = "https://lite.duckduckgo.com/lite/"
    params = {"q": query}

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(f"DuckDuckGo 返回 {resp.status_code}")
                return []

            # DuckDuckGo Lite 返回 HTML，需要用简单方式提取结果
            return _parse_ddg_lite(resp.text, max_results)

    except httpx.TimeoutException:
        logger.warning(f"DuckDuckGo 搜索超时: {query[:30]}")
        return []
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")
        return []


def _parse_ddg_lite(html: str, max_results: int) -> list[dict]:
    """解析 DuckDuckGo Lite 的 HTML 结果页"""
    results = []
    # 按 <a 标签提取结果
    import re
    # 查找结果区块：DuckDuckGo Lite 结构相对固定
    # 每个结果包含 class="result" 的 div

    # 更简单的提取：找链接和摘要
    lines = html.split("\n")
    current = {}
    in_result = False

    for line in lines:
        line = line.strip()
        # 匹配结果标题链接
        m = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', line)
        if m:
            if current.get("title"):
                results.append(current)
                if len(results) >= max_results:
                    break
            current = {"url": m.group(1), "title": re.sub(r'<[^>]+>', '', m.group(2)).strip(), "snippet": ""}
            in_result = True
            continue

        # 备选：更宽松的链接匹配
        if not current.get("title"):
            m = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', line)
            if m and "duckduckgo" not in m.group(1):
                url = m.group(1)
                title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if title and len(title) > 5:
                    current = {"url": url, "title": title, "snippet": ""}
                    in_result = True
                    continue

        # 提取摘要（<td> 或普通文本）
        if in_result and current.get("title"):
            snippet_match = re.search(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', line)
            if snippet_match:
                current["snippet"] = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip()
                # 完成一个结果
                results.append(current)
                if len(results) >= max_results:
                    break
                current = {}
                in_result = False

    # 最后的条目
    if current.get("title") and current.get("snippet"):
        results.append(current)

    return results[:max_results]


async def search(query: str, provider: str = "duckduckgo") -> str:
    """
    统一搜索接口，返回格式化的摘要文本供 LLM 消费。

    Returns: 格式化的搜索结果字符串，或空字符串
    """
    if provider == "duckduckgo":
        results = await search_duckduckgo(query)
    else:
        logger.warning(f"未知搜索提供者: {provider}")
        return ""

    if not results:
        return ""

    lines = [f"搜索结果: {query}", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        url = r.get("url", "")
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   {snippet}")
        if url:
            lines.append(f"   ({url})")
        lines.append("")

    return "\n".join(lines)


async def search_to_context(query: str, provider: str = "duckduckgo") -> str:
    """
    搜索并格式化为系统上下文。

    在 LLM 调用之前使用，将搜索结果注入 system prompt。
    """
    result = await search(query, provider)
    if result:
        return f"\n\n以下是联网搜索到的实时信息，请参考这些信息回答用户问题：\n{result}\n"
    return "\n\n【搜索无结果或搜索服务不可用，请根据已有知识回答】\n"
