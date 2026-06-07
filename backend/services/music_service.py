"""
音乐服务 —— 本地歌单 + 网易云(NeteaseCloudMusicApi) + Jamendo + Pixabay
网易云通过本地 Docker NeteaseCloudMusicApi 提供真实可播放 MP3 URL。
"""
import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# ── 在线搜索超时（快速失败，不阻塞播放） ──
SEARCH_TIMEOUT = 5  # 秒

# ── Jamendo API ──
JAMENDO_CLIENT_ID = "BEF1512782E87A4550ED94da5eBC44F6"
JAMENDO_TIMEOUT = 5


async def _search_jamendo(keyword: str, limit: int = 5) -> list[dict]:
    """
    Jamendo API — 返回真实可播放 mp3 URL。
    曲库：60万+ CC 授权独立音乐，含少量中文独立音乐。
    """
    if not keyword or not keyword.strip():
        return []

    try:
        params = {
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "limit": limit,
            "search": keyword.strip(),
            "include": "musicinfo",
            "audioformat": "mp32",
        }
        async with httpx.AsyncClient(timeout=JAMENDO_TIMEOUT) as client:
            resp = await client.get(
                "https://api.jamendo.com/v3.0/tracks/",
                params=params,
            )
            if resp.status_code != 200:
                logger.warning(f"Jamendo HTTP {resp.status_code}: {keyword}")
                return []

            data = resp.json()
            results = data.get("results", [])
            if not results:
                logger.info(f"Jamendo 无结果: {keyword}")
                return []

            songs = []
            seen = set()
            for item in results:
                song_id = str(item.get("id", ""))
                if song_id in seen:
                    continue
                seen.add(song_id)

                audio_url = item.get("audio", "")
                if not audio_url:
                    continue

                songs.append({
                    "song_id": f"jamendo_{song_id}",
                    "song_name": item.get("name", "未知曲目"),
                    "singers": item.get("artist_name", "独立音乐人"),
                    "album": item.get("album_name", ""),
                    "source": "jamendo",
                    "duration": "",
                    "duration_s": item.get("duration", 0),
                    "cover_url": item.get("image", ""),
                    "download_url": audio_url,
                    "ext": "mp3",
                    "file_size": "",
                    "file_size_bytes": 0,
                    "quality": "320kbps",
                    "lyric": "",
                    "bitrate": "",
                    "local": False,
                })

            if songs:
                logger.info(f"Jamendo '{keyword}': {len(songs)} 条（可播放）")
            return songs

    except asyncio.TimeoutError:
        logger.warning(f"Jamendo 超时: {keyword}")
        return []
    except Exception as e:
        logger.warning(f"Jamendo 异常: {keyword} - {e}")
        return []


async def _search_netease(keyword: str, limit: int = 10) -> list[dict]:
    """
    网易云音乐搜索（通过本地 Docker NeteaseCloudMusicApi）。
    返回标准化 SongInfo 列表，失败时返回空列表（不抛异常）。
    """
    if not keyword or not keyword.strip():
        return []

    try:
        from services.netease_cloud_api import get_netease_api
        api = get_netease_api()
        if not await api.check_available():
            logger.info("网易云 API 不可用，跳过")
            return []
        return await api.search_songs(keyword, limit)
    except ImportError:
        logger.warning("netease_cloud_api 模块未加载")
        return []
    except Exception as e:
        logger.warning(f"网易云搜索异常: {keyword} - {e}")
        return []


async def get_netease_play_url(song_id: str) -> str:
    """获取网易云歌曲的可播放 URL（实时获取，不缓存）"""
    try:
        from services.netease_cloud_api import get_netease_api
        api = get_netease_api()
        if not await api.check_available():
            return ""
        info = await api.get_play_url(song_id)
        return info.get("url", "")
    except Exception as e:
        logger.warning(f"获取网易云播放 URL 失败: {song_id} - {e}")
        return ""


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

    # 2. 🆕 网易云音乐（主力在线源，支持中文流行歌曲）
    netease_songs = await _search_netease(keyword)
    if netease_songs:
        # 为第一首歌获取可播放 URL
        first = netease_songs[0]
        raw_id = first["song_id"].replace("netease_", "")
        play_url = await get_netease_play_url(raw_id)
        if play_url:
            first["download_url"] = play_url
            logger.info(f"网易云命中（可播放）: {first['song_name']} - {first['singers']}")
            return netease_songs
        else:
            logger.info(f"网易云搜到但无播放链接: {first['song_name']} - {first['singers']}，回退下一级")

    # 3. Jamendo（CC 授权，有真实可播放 MP3 URL）
    jamendo_songs = await _search_jamendo(keyword)
    if jamendo_songs:
        return jamendo_songs

    # 4. 在线搜索（网易云 163 元数据，仅作参考）
    try:
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
        if resp.status_code != 200:
            logger.warning(f"在线搜索 HTTP {resp.status_code}: {keyword}")
            return []
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
                # 163 预览 URL 不可靠（302→404），不尝试解析
                play_url = ""

                songs.append({
                    "song_id": song_id,
                    "song_name": name,
                    "singers": artists,
                    "album": s.get('album', {}).get('name', '') if isinstance(s.get('album'), dict) else '',
                    "source": "netease",
                    "duration": "",
                    "duration_s": s.get('duration', 0) // 1000 if s.get('duration') else 0,
                    "cover_url": "",
                    "download_url": play_url,
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

    async def search_netease(self, keyword: str, limit: int = 10) -> list[dict]:
        return await _search_netease(keyword, limit)

    async def get_netease_play_url(self, song_id: str) -> str:
        return await get_netease_play_url(song_id)

    async def get_play_url(self, song_id: str, source: str, download_url: str = "", ext: str = "mp3") -> dict:
        return await get_play_url(song_id, source, download_url, ext)

    def get_cache_size(self) -> dict:
        return get_cache_size()

    def clear_cache(self):
        clear_cache()

    def is_available(self) -> bool:
        return is_available()
