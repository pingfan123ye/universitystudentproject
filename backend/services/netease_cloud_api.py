"""
网易云音乐 API 客户端 —— 封装对本地 Docker NeteaseCloudMusicApi 的 HTTP 调用。

NeteaseCloudMusicApi: https://github.com/Binaryify/NeteaseCloudMusicApi
Docker 镜像: binaryify/netease_cloud_music_api
默认端口: 3000

依赖环境变量:
  NETEASE_API_URL  — API 地址（默认 http://localhost:3000）
  NETEASE_COOKIE   — 登录 Cookie（MUSIC_U=xxx），/song/url 需要
"""
import asyncio
import logging
import os
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

# ── 配置 ──
NETEASE_API_URL = os.getenv("NETEASE_API_URL", "http://localhost:3000").rstrip("/")
NETEASE_COOKIE = os.getenv("NETEASE_COOKIE", "")
NETEASE_TIMEOUT = 3.0  # 本地服务，3 秒足够

# ── 单例 ──
_api_instance: "NeteaseCloudAPI | None" = None


def get_netease_api() -> "NeteaseCloudAPI":
    """获取网易云 API 客户端单例"""
    global _api_instance
    if _api_instance is None:
        _api_instance = NeteaseCloudAPI(NETEASE_API_URL, NETEASE_COOKIE)
    return _api_instance


class NeteaseCloudAPI:
    """网易云音乐 API 客户端"""

    def __init__(self, base_url: str = NETEASE_API_URL, cookie: str = NETEASE_COOKIE):
        self.base_url = base_url
        self.cookie = cookie
        self._available: bool | None = None  # None=未检测, True=可用, False=不可用

    @property
    def available(self) -> bool | None:
        return self._available

    def _headers(self) -> dict:
        """构建请求头（含 Cookie）"""
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    # ═══════════════════════════════════════════════════════════
    # 可用性检测
    # ═══════════════════════════════════════════════════════════

    async def check_available(self) -> bool:
        """检测 API 服务是否可达（轻量 ping）"""
        if self._available is True:
            return True
        try:
            async with httpx.AsyncClient(timeout=NETEASE_TIMEOUT) as client:
                resp = await client.get(self.base_url, headers=self._headers())
                # API 根路径返回 HTML 页面或 JSON，只要状态码 200 即表示运行中
                if 200 <= resp.status_code < 500:
                    self._available = True
                    logger.info(f"网易云 API 可用: {self.base_url}")
                    return True
                self._available = False
                logger.warning(f"网易云 API 异常状态码: {resp.status_code}")
                return False
        except httpx.TimeoutException:
            self._available = False
            logger.warning(f"网易云 API 超时: {self.base_url}")
            return False
        except Exception as e:
            self._available = False
            logger.warning(f"网易云 API 不可达: {e}")
            return False

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        """内部 GET 请求封装"""
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=NETEASE_TIMEOUT) as client:
                resp = await client.get(url, params=params, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning(f"网易云 API HTTP {resp.status_code}: {path}")
                    return None
                data = resp.json()
                if data.get("code") != 200:
                    logger.warning(f"网易云 API 业务错误 code={data.get('code')}: {path} msg={data.get('message', '')}")
                    return None
                return data
        except httpx.TimeoutException:
            logger.warning(f"网易云 API 请求超时: {path}")
            return None
        except Exception as e:
            logger.warning(f"网易云 API 请求异常: {path} - {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # 歌曲搜索
    # ═══════════════════════════════════════════════════════════

    async def search_songs(self, keyword: str, limit: int = 10) -> list[dict]:
        """
        搜索歌曲 → GET /search?keywords=xxx&type=1&limit=10
        返回标准化 SongInfo 列表（按质量排序：原唱优先、免费优先、热度优先）
        """
        if not keyword or not keyword.strip():
            return []

        # 请求更多结果（30条），以便本地排序筛选后取 top limit
        data = await self._get("/search", params={
            "keywords": keyword.strip(),
            "type": "1",       # 单曲
            "limit": "30",
        })
        if not data:
            return []

        songs = data.get("result", {}).get("songs", [])
        if not songs:
            logger.info(f"网易云搜索无结果: '{keyword}'")
            return []

        # ── 排序前预处理：提取关键词中的歌手名用于精确匹配加分 ──
        kw_lower = keyword.strip().lower()
        # 提取可能的中文歌手名（取前4个字符尝试匹配）
        artist_terms = []
        for sep in ["的", " ", "·", "-", "—"]:
            if sep in keyword:
                parts = keyword.split(sep)
                if len(parts) >= 2:
                    artist_terms.append(parts[0].strip().lower())
                break

        results = []
        seen = set()
        for s in songs:
            song_id = str(s.get("id", ""))
            if not song_id or song_id in seen:
                continue
            seen.add(song_id)

            # 歌手名
            artists = s.get("ar", s.get("artists", []))
            if isinstance(artists, list):
                artist_names = [a.get("name", "") for a in artists if a.get("name")]
                singer = " / ".join(artist_names)
                artist_lower = " ".join(artist_names).lower()
            else:
                singer = str(artists) if artists else ""
                artist_lower = singer.lower()

            # 歌曲名
            song_name = s.get("name", "未知曲目")

            # 专辑名
            album = s.get("al", s.get("album", {}))
            if isinstance(album, dict):
                album_name = album.get("name", "")
                cover_url = (album.get("picUrl", "") or "").replace(
                    "http://", "https://"
                )
            else:
                album_name = ""
                cover_url = ""

            # 时长（毫秒 → 秒）
            duration_ms = s.get("dt", s.get("duration", 0))
            duration_s = duration_ms // 1000 if duration_ms else 0
            mins = duration_s // 60
            secs = duration_s % 60
            duration_str = f"{mins}:{secs:02d}" if duration_s > 0 else ""

            # 质量元数据（用于排序）
            fee = s.get("fee", 0)          # 0=免费, 1=VIP, 8=付费
            pop = s.get("pop", 0) or 0     # 热度 0-100

            # ── 排序得分（越高越好）──
            # 1. 过滤试听片段（<30秒 可能是不完整版本）
            if duration_s < 30 and duration_s > 0:
                continue

            # 2. 计算得分
            score = 0
            # 免费歌曲 +100 分
            if fee == 0:
                score += 100
            # 热度（0-100）+ 直接加分
            score += pop
            # 歌手名匹配关键词中的艺术家名 +200 分
            if artist_terms:
                for term in artist_terms:
                    if term in artist_lower:
                        score += 200
                        break
            # 歌名精确包含关键词 +50 分
            if kw_lower in song_name.lower() or kw_lower.replace(" ", "") in song_name.lower().replace(" ", ""):
                score += 50
            # 时长合理的完整歌曲（>2分钟） +30 分
            if duration_s > 120:
                score += 30

            results.append({
                "song_id": f"netease_{song_id}",
                "song_name": song_name,
                "singers": singer or "未知歌手",
                "album": album_name,
                "source": "netease",
                "duration": duration_str,
                "duration_s": duration_s,
                "cover_url": cover_url,
                "download_url": "",  # 播放前通过 get_play_url() 实时获取
                "ext": "mp3",
                "file_size": "",
                "file_size_bytes": 0,
                "quality": "",
                "lyric": "",
                "local": False,
                "_score": score,  # 内部排序用，不对外暴露
            })

        # 按得分降序排列
        results.sort(key=lambda x: x.get("_score", 0), reverse=True)

        # 移除内部字段并截断到 limit
        for r in results:
            r.pop("_score", None)
        results = results[:limit]

        if results:
            logger.info(f"网易云搜索 '{keyword}': {len(results)} 条 (已排序)")
        return results

    # ═══════════════════════════════════════════════════════════
    # 播放 URL
    # ═══════════════════════════════════════════════════════════

    async def get_play_url(self, song_id: str | int, level: str = "standard") -> dict:
        """
        获取歌曲播放 URL → GET /song/url?id=xxx&level=standard

        参数:
          song_id: 网易云歌曲 ID（纯数字，不含 "netease_" 前缀）
          level: 音质 standard|higher|exhigh|lossless|hires

        返回:
          {"url": "https://...", "br": 320000, "type": "mp3", "size": 1234567}
          或 {"url": "", ...} (无播放权限/VIP歌曲)
        """
        # 去掉可能带的前缀
        raw_id = str(song_id).replace("netease_", "")

        data = await self._get("/song/url", params={
            "id": raw_id,
            "level": level,
        })
        if not data:
            return {"url": "", "br": 0, "type": "", "size": 0}

        items = data.get("data", [])
        if not items:
            logger.info(f"网易云 song/url 空数据: id={raw_id}")
            return {"url": "", "br": 0, "type": "", "size": 0}

        item = items[0]
        url = item.get("url", "") or ""
        if not url:
            logger.info(f"网易云歌曲无播放链接: id={raw_id} (可能是 VIP/无版权)")
            return {
                "url": "",
                "br": item.get("br", 0),
                "type": item.get("type", ""),
                "size": item.get("size", 0),
            }

        # 强制 HTTPS
        if url.startswith("http://"):
            url = url.replace("http://", "https://", 1)

        return {
            "url": url,
            "br": item.get("br", 0),
            "type": item.get("type", "mp3"),
            "size": item.get("size", 0),
        }

    # ═══════════════════════════════════════════════════════════
    # 歌单
    # ═══════════════════════════════════════════════════════════

    async def get_playlist(self, playlist_id: str | int) -> list[dict]:
        """
        获取歌单全部歌曲 → GET /playlist/detail?id=xxx
        返回标准化 SongInfo 列表
        """
        data = await self._get("/playlist/detail", params={"id": str(playlist_id)})
        if not data:
            return []

        playlist = data.get("playlist", {})
        tracks = playlist.get("tracks", [])
        if not tracks:
            logger.info(f"网易云歌单为空: id={playlist_id}")
            return []

        songs = []
        seen = set()
        for t in tracks:
            tid = str(t.get("id", ""))
            if not tid or tid in seen:
                continue
            seen.add(tid)

            artists = t.get("ar", [])
            singer = " / ".join(a.get("name", "") for a in artists if a.get("name"))

            album = t.get("al", {})
            album_name = album.get("name", "") if isinstance(album, dict) else ""
            cover_url = (album.get("picUrl", "") if isinstance(album, dict) else "").replace(
                "http://", "https://"
            )

            duration_ms = t.get("dt", 0)
            duration_s = duration_ms // 1000
            mins = duration_s // 60
            secs = duration_s % 60
            duration_str = f"{mins}:{secs:02d}" if duration_s > 0 else ""

            songs.append({
                "song_id": f"netease_{tid}",
                "song_name": t.get("name", "未知曲目"),
                "singers": singer or "未知歌手",
                "album": album_name,
                "source": "netease",
                "duration": duration_str,
                "duration_s": duration_s,
                "cover_url": cover_url,
                "download_url": "",
                "ext": "mp3",
                "file_size": "",
                "file_size_bytes": 0,
                "quality": "",
                "lyric": "",
                "local": False,
            })

        logger.info(f"网易云歌单 #{playlist_id}: {len(songs)} 首")
        return songs

    async def search_playlists(self, keyword: str, limit: int = 5) -> list[dict]:
        """
        搜索歌单 → GET /search?keywords=xxx&type=1000
        返回歌单摘要列表 [{playlist_id, name, track_count, cover_url}]
        """
        if not keyword or not keyword.strip():
            return []

        data = await self._get("/search", params={
            "keywords": keyword.strip(),
            "type": "1000",     # 歌单
            "limit": str(limit),
        })
        if not data:
            return []

        playlists = data.get("result", {}).get("playlists", [])
        results = []
        for pl in playlists:
            cover = (pl.get("coverImgUrl", "") or "").replace("http://", "https://")
            results.append({
                "playlist_id": str(pl.get("id", "")),
                "name": pl.get("name", ""),
                "track_count": pl.get("trackCount", 0),
                "play_count": pl.get("playCount", 0),
                "cover_url": cover,
                "creator": (pl.get("creator", {}) or {}).get("nickname", ""),
            })

        if results:
            logger.info(f"网易云歌单搜索 '{keyword}': {len(results)} 个歌单")
        return results


# ═══════════════════════════════════════════════════════════
# 便捷函数（供外部直接 import 使用）
# ═══════════════════════════════════════════════════════════

async def search_netease_songs(keyword: str, limit: int = 10) -> list[dict]:
    """搜索网易云歌曲（便捷函数）"""
    api = get_netease_api()
    if not await api.check_available():
        return []
    return await api.search_songs(keyword, limit)


async def get_netease_play_url(song_id: str | int) -> str:
    """获取网易云歌曲的可播放 URL（便捷函数）"""
    api = get_netease_api()
    if not await api.check_available():
        return ""
    info = await api.get_play_url(song_id)
    return info.get("url", "")


async def get_netease_playlist(playlist_id: str | int) -> list[dict]:
    """获取网易云歌单歌曲（便捷函数）"""
    api = get_netease_api()
    if not await api.check_available():
        return []
    songs = await api.get_playlist(playlist_id)
    # 为歌单中的每首歌获取播放 URL
    for song in songs:
        raw_id = song["song_id"].replace("netease_", "")
        info = await api.get_play_url(raw_id)
        if info.get("url"):
            song["download_url"] = info["url"]
    return songs
