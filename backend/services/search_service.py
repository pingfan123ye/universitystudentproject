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
        "t": "smart_speaker",  # 标识客户端类型，减少 202
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 202:
                # 202 常因地区限制/频率限制，尝试 HTML 版作为回退
                logger.warning("DuckDuckGo API 返回 202，尝试 HTML 回退...")
                html_resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    timeout=SEARCH_TIMEOUT,
                )
                if html_resp.status_code == 200:
                    text = html_resp.text[:2000]
                    logger.info("DuckDuckGo HTML 回退获取到 %d 字符", len(text))
                    # HTML 回退能拿到原始结果页面，但无法结构化解析
                    # 返回简要提示让 LLM 知道搜索未成功获取结构化数据
                    return []
                logger.warning("DuckDuckGo HTML 回退也失败: %d", html_resp.status_code)
                return []
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
    return (
        "\n\n[注意: 联网搜索暂时不可用。"
        "如果用户问的是实时信息(天气/新闻等), 请自然地回复说你暂时查不到最新信息。"
        "如果用户问的是知识类问题, 请直接根据你的知识回答, 不要说你查不到。"
        "绝对不要提及搜索失败、API错误等技术细节。]\n"
    )
