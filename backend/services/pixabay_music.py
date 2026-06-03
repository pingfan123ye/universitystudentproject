"""
Pixabay Music 爬虫 —— 从 pixabay.com/music/ 搜索结果中提取可播放 MP3 URL。
Pixabay 音乐可免费下载，适合作为泛化请求（"放点轻松音乐"）的音源。
"""
import asyncio
import logging
import re
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

PIXABAY_MUSIC_URL = "https://pixabay.com/music/search/"
SEARCH_TIMEOUT = 8


async def search_pixabay_music(query: str, limit: int = 5) -> list[dict]:
    """
    爬取 Pixabay Music 搜索结果，提取含可播放 MP3 URL 的歌曲列表。
    """
    if not query or not query.strip():
        return []

    search_url = PIXABAY_MUSIC_URL + urllib.parse.quote(query.strip())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(search_url)
            if resp.status_code != 200:
                logger.warning(f"Pixabay Music HTTP {resp.status_code}: {query}")
                return []

            html = resp.text
            songs = _parse_search_results(html)

            if songs:
                logger.info(f"Pixabay Music '{query}': {len(songs)} 条")
            else:
                logger.info(f"Pixabay Music '{query}': 无结果")
            return songs[:limit]

    except httpx.TimeoutException:
        logger.warning(f"Pixabay Music 超时: {query}")
        return []
    except Exception as e:
        logger.warning(f"Pixabay Music 异常: {e}")
        return []


def _parse_search_results(html: str) -> list[dict]:
    """从 Pixabay Music 搜索结果 HTML 中提取歌曲信息"""
    songs = []

    # 方法1: 查找 JSON-LD / schema.org 结构化数据
    json_ld_pattern = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for ld in json_ld_pattern:
        try:
            import json
            data = json.loads(ld)
            if isinstance(data, list):
                for item in data:
                    song = _extract_from_schema(item)
                    if song:
                        songs.append(song)
            else:
                song = _extract_from_schema(data)
                if song:
                    songs.append(song)
        except (json.JSONDecodeError, KeyError):
            pass

    if songs:
        return _dedupe(songs)

    # 方法2: 查找 audio 标签及其 data- 属性
    audio_matches = re.findall(
        r'<audio[^>]*data-src="([^"]+)"[^>]*data-title="([^"]*)"[^>]*>',
        html, re.IGNORECASE,
    )
    for url, title in audio_matches:
        if url and url.endswith(('.mp3', '.wav', '.ogg')):
            songs.append({
                "song_id": f"pixabay_{hash(url)}",
                "song_name": _decode_html(title) or "未知曲目",
                "singers": "Pixabay",
                "album": "",
                "source": "pixabay",
                "duration": "",
                "duration_s": 0,
                "cover_url": "",
                "download_url": url,
                "ext": url.rsplit('.', 1)[-1] if '.' in url else 'mp3',
                "file_size": "",
                "file_size_bytes": 0,
                "quality": "",
                "lyric": "",
                "local": False,
            })

    if songs:
        return _dedupe(songs)

    # 方法3: 查找通用的 MP3 URL 模式 + 附近标题
    # Pixabay 搜索结果页面中，每条结果通常在一个容器元素内
    # 查找所有 .mp3 链接及其周围的文本
    mp3_urls = re.findall(
        r'(https?://[^"\s<>]+\.mp3(?:\?[^"\s<>]*)?)',
        html, re.IGNORECASE,
    )
    # 查找标题（通常在 <a class="..."> 标签中，紧邻音频链接）
    title_matches = re.findall(
        r'<a[^>]*class="[^"]*track[^"]*"[^>]*>([^<]+)</a>',
        html, re.IGNORECASE,
    )
    # 更宽泛的标题搜索
    if not title_matches:
        title_matches = re.findall(
            r'<a[^>]*class="[^"]*(?:title|name|track)[^"]*"[^>]*>\s*([^<]{2,80})\s*</a>',
            html, re.IGNORECASE,
        )

    for i, url in enumerate(mp3_urls):
        title = title_matches[i] if i < len(title_matches) else f"{query} #{i+1}"
        title = _decode_html(title).strip()
        songs.append({
            "song_id": f"pixabay_{hash(url)}",
            "song_name": title or f"Pixabay {query} #{i+1}",
            "singers": "Pixabay",
            "album": "",
            "source": "pixabay",
            "duration": "",
            "duration_s": 0,
            "cover_url": "",
            "download_url": url,
            "ext": "mp3",
            "file_size": "",
            "file_size_bytes": 0,
            "quality": "",
            "lyric": "",
            "local": False,
        })

    return _dedupe(songs)


def _extract_from_schema(item: dict) -> dict | None:
    """从 schema.org JSON-LD 中提取音乐信息"""
    item_type = item.get("@type", "")
    if item_type not in ("MusicRecording", "AudioObject", "MusicPlaylist"):
        return None

    url = item.get("contentUrl", "") or item.get("url", "") or item.get("embedUrl", "")
    if isinstance(url, list):
        url = url[0] if url else ""
    if not url or not url.endswith(('.mp3', '.wav', '.ogg', '.m4a')):
        return None

    name = item.get("name", "") or item.get("headline", "")
    artist = ""
    if isinstance(item.get("author"), dict):
        artist = item["author"].get("name", "")
    elif isinstance(item.get("creator"), dict):
        artist = item["creator"].get("name", "")
    elif item.get("artist"):
        artist = str(item["artist"])

    return {
        "song_id": f"pixabay_{hash(url)}",
        "song_name": name or "未知曲目",
        "singers": artist or "Pixabay",
        "album": "",
        "source": "pixabay",
        "duration": "",
        "duration_s": 0,
        "cover_url": item.get("thumbnailUrl", ""),
        "download_url": url,
        "ext": url.rsplit('.', 1)[-1] if '.' in url else 'mp3',
        "file_size": "",
        "file_size_bytes": 0,
        "quality": "",
        "lyric": "",
        "local": False,
    }


def _decode_html(text: str) -> str:
    """解码 HTML 实体"""
    import html
    return html.unescape(text)


def _dedupe(songs: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for s in songs:
        key = s["download_url"]
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def is_available() -> bool:
    return True
