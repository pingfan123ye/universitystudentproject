"""
联网搜索服务 —— 为 LLM 提供实时信息检索
使用 DuckDuckGo Instant Answer API（免费，无需 API Key）。
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 5
MAX_RESULTS = 5


async def search_duckduckgo(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    """
    通过 DuckDuckGo Instant Answer API 搜索。

    Returns: [{"title": str, "snippet": str, "url": str}, ...]
    """
    url = "https://api.duckduckgo.com/"
    params = {
        "q": query,
        "format": "json",
        "no_html": 1,
        "skip_disambig": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(f"DuckDuckGo API 返回 {resp.status_code}")
                return []

            data = resp.json()
            results = []

            # AbstractText 是 DDG 的摘要
            abstract = data.get("AbstractText", "")
            abstract_url = data.get("AbstractURL", "")
            if abstract:
                results.append({
                    "title": data.get("Heading", query),
                    "snippet": abstract,
                    "url": abstract_url,
                })

            # RelatedTopics 是相关话题
            for topic in data.get("RelatedTopics", []):
                if isinstance(topic, dict) and "Text" in topic:
                    text = topic["Text"]
                    url_topic = topic.get("FirstURL", "")
                    results.append({
                        "title": text.split(" - ")[0] if " - " in text else text[:50],
                        "snippet": text,
                        "url": url_topic,
                    })
                elif isinstance(topic, dict) and "Topics" in topic:
                    for subtopic in topic["Topics"]:
                        if isinstance(subtopic, dict) and "Text" in subtopic:
                            text = subtopic["Text"]
                            results.append({
                                "title": text.split(" - ")[0] if " - " in text else text[:50],
                                "snippet": text,
                                "url": subtopic.get("FirstURL", ""),
                            })

            return results[:max_results]

    except httpx.TimeoutException:
        logger.warning(f"DuckDuckGo 搜索超时: {query[:30]}")
        return []
    except Exception as e:
        logger.warning(f"DuckDuckGo 搜索失败: {e}")
        return []


async def search(query: str, provider: str = "duckduckgo") -> str:
    """
    统一搜索接口，返回格式化的摘要文本供 LLM 消费。
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
    """
    result = await search(query, provider)
    if result:
        return f"\n\n以下是联网搜索到的实时信息，请参考这些信息回答用户问题：\n{result}\n"
    return "\n\n【搜索无结果或搜索服务不可用，请根据已有知识回答】\n"
