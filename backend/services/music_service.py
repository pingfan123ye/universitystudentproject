"""
音乐服务 —— 专注本地歌单 + 极简在线搜索
在线源（Netease）搜索只返回元数据，无 playable URL。
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

# ── 在线搜索超时（快速失败，不阻塞播放） ──
SEARCH_TIMEOUT = 5  # 秒


async def search_songs(keyword: str, sources: list[str] | None = None) -> list[dict]:
    """
    搜索歌曲。优先级：本地歌单 → 在线元数据搜索。
    在线搜索只返回歌曲信息（不含可播放 URL），用于填充队列。
    """
    if not keyword or not keyword.strip():
        return []

    # 1. 本地歌单（毫秒级，零网络）
    try:
        import sys as _sys, os as _os
        _backend_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _backend_dir not in _sys.path:
            _sys.path.insert(0, _backend_dir)
        from playlist_service import search_local
        local = search_local(keyword)
    except ImportError:
        local = None
    if local:
        logger.info(f"本地歌单命中: {local['song_name']} - {local['artist']}")
        return [{
            "song_id": f"local_{local['filename']}",
            "song_name": local["song_name"],
            "singers": local["artist"],
            "album": "",
            "source": "local",
            "duration": "",
            "duration_s": 0,
            "cover_url": "",
            "download_url": local["url"],
            "ext": "mp3",
            "file_size": "",
            "file_size_bytes": 0,
            "quality": "",
            "lyric": "",
            "bitrate": "",
            "local": True,
        }]

    # 2. 在线搜索（仅搜索元数据，无 playable URL）
    try:
        import httpx
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://music.163.com/'
        }
        api_url = f'https://music.163.com/api/search/get?s={keyword}&limit=5&offset=0&type=1'
        loop = asyncio.get_event_loop()
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: httpx.get(api_url, headers=headers, timeout=8)),
            timeout=SEARCH_TIMEOUT
        )
        data = resp.json()
        if data.get('code') == 200 and data.get('result', {}).get('songs'):
            songs = []
            seen = set()
            for s in data['result']['songs']:
                song_id = str(s['id'])
                if song_id in seen:
                    continue
                seen.add(song_id)
                name = s['name']
                artists = ','.join([a['name'] for a in s.get('artists', [])])
                # Netease 不再提供免费播放链接，download_url 为空
                # 前端会 fallback 到默认歌曲
                songs.append({
                    "song_id": song_id,
                    "song_name": name,
                    "singers": artists,
                    "album": s.get('album', {}).get('name', '') if isinstance(s.get('album'), dict) else '',
                    "source": "netease",
                    "duration": "",
                    "duration_s": s.get('duration', 0) // 1000 if s.get('duration') else 0,
                    "cover_url": "",
                    "download_url": "",  # 无可用播放链接
                    "ext": "mp3",
                    "file_size": "",
                    "file_size_bytes": 0,
                    "quality": "",
                    "lyric": "",
                    "bitrate": "",
                })
                if len(songs) >= 5:
                    break
            logger.info(f"在线搜索 '{keyword}': {len(songs)} 条（无播放链接）")
            return songs
    except asyncio.TimeoutError:
        logger.warning(f"在线搜索超时: {keyword}")
    except Exception as e:
        logger.warning(f"在线搜索失败: {keyword} - {e}")

    logger.info(f"搜索无结果: {keyword}")
    return []


async def get_play_url(song_id: str, source: str, download_url: str = "", ext: str = "mp3") -> dict:
    """获取播放 URL（本地歌曲直接返回，网络歌曲暂无）"""
    if download_url and download_url.startswith("/"):
        return {"url": download_url, "cached": False, "ext": ext, "expires_in": 0}
    return {"url": "", "cached": False, "ext": "", "expires_in": 0}


def get_cache_size() -> dict:
    return {"file_count": 0, "total_bytes": 0, "cache_dir": ""}


def clear_cache():
    pass


def is_available() -> bool:
    return True


class MusicService:
    """音乐服务门面类（兼容旧调用方 MusicService().search_songs() 写法）"""

    async def search_songs(self, keyword: str, sources: list[str] | None = None) -> list[dict]:
        return await search_songs(keyword, sources)

    async def get_play_url(self, song_id: str, source: str, download_url: str = "", ext: str = "mp3") -> dict:
        return await get_play_url(song_id, source, download_url, ext)

    def get_cache_size(self) -> dict:
        return get_cache_size()

    def clear_cache(self):
        clear_cache()

    def is_available(self) -> bool:
        return is_available()
