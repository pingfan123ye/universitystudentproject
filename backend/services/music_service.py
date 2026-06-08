"""
音乐服务 —— 本地歌单 + 网易云(NeteaseCloudMusicApi) + Jamendo + Pixabay
网易云通过本地 Docker NeteaseCloudMusicApi 提供真实可播放 MP3 URL。

音乐控制 API:
  send_music_control(ws, music_action, tts_callback) — 核心音乐播放/控制入口
  clean_music_query(text) — 统一搜索词清洗
  _user_requested_music(user_prompt) — 交叉验证用户是否真的请求了音乐
  _announce_and_play(ws, song, queue_songs, tts_callback) — 播放+播报
"""
import asyncio
import logging
import os
import re as _re
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


# ═══════════════════════════════════════════════════════════
# 搜索词清洗（统一入口）
# ═══════════════════════════════════════════════════════════

def clean_music_query(text: str) -> str:
    """清理搜索词：移除唤醒词、指令词、礼貌前缀、语气词，只保留歌名关键词。

    合并了原 _clean_song_query (main.py) 和 _extract_music_query (intent_router.py) 的逻辑。
    """
    # 1. 去掉唤醒词前缀
    cleaned = _re.sub(r"^(小智小智|小智|小字小字|小字|小子小子|小子)\s*(帮我|给我|來|来|)\s*", "", text)
    # 2. 去掉礼貌/填充前缀
    cleaned = _re.sub(r"^(好的|好|帮我|给我|能不能|可不可以|可以|麻烦你|请你|請你|幫我|給我|帮忙|幫忙|请|請|那个|这个)\s*", "", cleaned)
    # 3. 去掉指令词前缀
    cleaned = _re.sub(r"^(播放|放|来一首|我想听|我要听|点一首|给我放|给我播|播|来首|来点|想听|听)\s*", "", cleaned)
    # 4. 去掉任意位置的唤醒词（如"我想听歌了小智"中的"小智"）
    cleaned = _re.sub(r"\s*(小智小智|小智|小字小字|小字|小子小子|小子)\s*", " ", cleaned)
    # 5. 去掉首尾虚词
    cleaned = _re.sub(r"^[了吧啊呀哦啦呗么嗯了]+", "", cleaned)
    cleaned = _re.sub(r"[了吧啊呀哦啦呗么嗯了]+$", "", cleaned)
    # 6. 去掉末尾的"的歌"、"的音乐"、"的歌曲"（intent_router 版逻辑）
    cleaned = _re.sub(r'的(歌|音乐|歌曲)*$', '', cleaned).strip()
    # 7. 去掉独立的单字代词（仅当它们前后是空格或边界时才移除）
    cleaned = _re.sub(r'(?:^|\s)[我你他她它](?=\s|$)', ' ', cleaned)
    # 8. 去掉口语功能词（永远不可能是歌名/歌手名的词）
    #    不使用 \b（中文连续字之间 \b 不匹配），改用直接替换
    cleaned = _re.sub(
        r'(好的|进行|一下|那个|这个|选首|选个|来帮我|帮我选|选手|选手给|随机|随机多|多放|多|我让|我讓|让你|讓你|随便|隨便|随意|隨意|字幕)',
        ' ', cleaned)
    # 9. 去除中英文标点符号（避免污染搜索词，如"双截棍。"→"双截棍"）
    cleaned = _re.sub(r'[。，！？、；：""''《》【】…—～.!?,;:\'\"(){}$]', '', cleaned)
    # 10. 合并多余空格
    cleaned = _re.sub(r'\s+', ' ', cleaned).strip()
    # 11. 纯泛化请求 → 返回空串让上游随机播放
    if _re.match(
        r'^(歌曲|歌|音乐|音樂|一首歌|几首歌|来首歌|随便|随便来|随便听|随便听歌|来点音乐|什么歌|啥歌|点儿歌|点歌|听歌|想听歌|'
        r'什么都行|啥都行|都可以|随便什么|什么都可|随意|随机|任意|听音乐|聽音樂|想听|想聽|好的|好|行|可以|行吧|好吧|'
        r'一首|换一首|随便一首|放一首|随便一首|来点|来点歌|随便来首)\s*$',
        cleaned):
        return ""
    # 12. 太短（单字）或太长（>40字）不靠谱
    if len(cleaned) < 2 or len(cleaned) > 40:
        return ""
    return cleaned


# ═══════════════════════════════════════════════════════════
# 辅助工具
# ═══════════════════════════════════════════════════════════

def _is_chinese_query(query: str) -> bool:
    """检测 query 是否包含中文字符（用于版权拦截判断）"""
    return bool(_re.search(r'[一-鿿]', query))


def _mood_to_english(query: str) -> str:
    """中文情绪/场景词 → Jamendo 英文标签（轻音乐/纯音乐等 Jamendo 中文无结果）"""
    if not query:
        return ""
    q = query.lower()
    if any(kw in q for kw in ["轻音乐", "纯音乐", "纯音", "轻音"]):
        return "instrumental piano"
    if any(kw in q for kw in ["安静", "不吵", "环境", "背景", "看书", "读书", "专注", "专心"]):
        return "ambient study"
    if any(kw in q for kw in ["学习", "看书", "读书", "做作业", "写作业"]):
        return "study focus"
    if any(kw in q for kw in ["放松", "舒缓", "安静", "平静", "轻松"]):
        return "relaxing calm"
    if any(kw in q for kw in ["钢琴", "吉他", "古筝", "琵琶", "二胡", "小提琴"]):
        return "piano instrumental"
    if any(kw in q for kw in ["冥想", "瑜伽", "睡眠"]):
        return "meditation sleep"
    if any(kw in q for kw in ["自然", "下雨", "雨声", "海浪", "鸟"]):
        return "nature sounds"
    if any(kw in q for kw in ["咖啡", "咖啡馆", "咖啡厅"]):
        return "coffee shop jazz"
    return ""


# ═══════════════════════════════════════════════════════════
# 网易云工具
# ═══════════════════════════════════════════════════════════

async def _batch_fetch_netease_urls(songs: list[dict], count: int = 5) -> list[dict]:
    """为网易云搜索结果中的前 N 首歌并发获取可播放 URL。
    只修改有 download_url 为空的歌曲，已有 URL 的跳过。"""
    from services.netease_cloud_api import get_netease_api

    api = get_netease_api()
    if not await api.check_available():
        return songs

    targets = []
    for s in songs:
        raw_id = s.get("song_id", "").replace("netease_", "")
        if raw_id and s.get("source") == "netease" and not s.get("download_url"):
            targets.append((s, raw_id))
        if len(targets) >= count:
            break

    if not targets:
        return songs

    async def _fetch(song, raw_id):
        try:
            info = await api.get_play_url(raw_id)
            if info.get("url"):
                song["download_url"] = info["url"]
        except Exception:
            pass

    await asyncio.gather(*[_fetch(s, rid) for s, rid in targets])
    fetched = sum(1 for s, _ in targets if s.get("download_url"))
    if fetched:
        logger.info(f"网易云批量获取 URL: {fetched}/{len(targets)} 首")
    return songs


# ═══════════════════════════════════════════════════════════
# 本地歌曲工具
# ═══════════════════════════════════════════════════════════

def _build_local_song(picked: dict) -> dict:
    return {
        "song_id": f"local_{picked['filename']}",
        "song_name": picked["song_name"], "singers": picked["artist"],
        "album": "", "source": "local", "duration": "", "duration_s": 0,
        "cover_url": "", "download_url": picked["url"], "ext": "mp3",
        "file_size": "", "file_size_bytes": 0, "quality": "", "lyric": "",
    }


# ═══════════════════════════════════════════════════════════
# 音乐请求交叉验证：防止 LLM 幻觉式 ACTIONS 标签
# ═══════════════════════════════════════════════════════════
# 用户必须明确说了音乐相关指令，否则即使 LLM 输出音乐 ACTIONS 也跳过。
# 这是针对小模型（qwen2.5:7b）将回复中的短语（如"相信自己"）幻觉成歌曲名的防线。
_MUSIC_REQUEST_PATTERNS = [
    "播放", "放歌", "放音乐", "放点音乐", "放点歌", "放首歌", "放个歌", "放一首", "放个音乐",
    "来首歌", "来点音乐", "来点歌", "来首", "来一首", "来一曲", "来一段音乐",
    "听", "听歌", "听音乐", "听首", "想听歌", "想听音乐", "想听",
    "播歌", "播音乐", "播点音乐", "播点歌", "播一首",
    "点歌", "点一首", "点首",
    "唱首歌", "唱歌",
    "切歌", "下一首", "下一曲", "上一首", "上一曲", "换一首", "换个歌",
    "暂停音乐", "暂停播放", "暂停", "停止播放", "停止音乐",
    "继续播放", "继续",
    "关掉音乐", "关闭音乐", "关音乐", "别放了", "别唱了",
    "聽歌", "聽音樂", "想聽", "放音樂", "播放音樂", "播放歌曲",
    "暫停音樂", "繼續播放",
    "随便来一首", "随便放", "随机播放", "随机来一首",
    "来点背景音乐", "放点背景音乐", "放背景音乐",
    "放点", "来点", "歌单", "收藏", "轻音乐",
]

# "听"字的非音乐短语排除（听不懂、听不清、听说…）
_MUSIC_TING_EXCLUSIONS = (
    "听不懂", "听不清", "听不到", "听不见", "听说", "听起来",
)


def _user_requested_music(user_prompt: str) -> bool:
    """检查用户原文是否包含音乐播放/控制请求。
    返回 True 表示用户确实在请求音乐操作，False 表示 LLM 很可能在幻觉。
    """
    if not user_prompt or not user_prompt.strip():
        return False
    for kw in _MUSIC_REQUEST_PATTERNS:
        if kw in user_prompt:
            if kw == "听":
                for excl in _MUSIC_TING_EXCLUSIONS:
                    if excl in user_prompt:
                        return False
            return True
    return False


# ═══════════════════════════════════════════════════════════
# 播放与播报
# ═══════════════════════════════════════════════════════════

async def _announce_and_play(ws, song: dict, queue_songs: list[dict] | None = None, tts_callback=None):
    """发送播放指令 + TTS 语音播报歌名。
    当 queue_songs 提供时，将其打包为完整队列发给前端（当前歌曲排第一，其余随机）。
    队列中只保留有 download_url 的歌曲，确保前端切歌不会落到空 URL。"""
    name = song.get("song_name", "")
    singer = song.get("singers", "")

    if queue_songs:
        import random as _random
        current_id = song.get("song_id", "")
        others = [s for s in queue_songs
                  if s.get("song_id") != current_id
                  and (s.get("download_url") or s.get("source") == "local")]
        _random.shuffle(others)
        songs_payload = [song] + others
    else:
        songs_payload = [song]

    await ws.send_json({
        "type": "music_control", "action": "play",
        "song_id": song.get("song_id", ""),
        "song_name": name,
        "singers": singer,
        "album": song.get("album", ""),
        "source": song.get("source", ""),
        "duration": song.get("duration", ""),
        "duration_s": song.get("duration_s", 0),
        "cover_url": song.get("cover_url", ""),
        "download_url": song.get("download_url", ""),
        "ext": song.get("ext", "mp3"),
        "songs": songs_payload,
    })
    if singer:
        announce_text = f"好的，为你播放{singer}的{name}"
    else:
        announce_text = f"好的，为你播放{name}"
    if tts_callback:
        asyncio.create_task(tts_callback(ws, announce_text, "music"))
    logger.info(f"播放: {name} - {singer}" + (f" (队列 {len(songs_payload)} 首)" if queue_songs else ""))


# ═══════════════════════════════════════════════════════════
# 核心音乐控制入口
# ═══════════════════════════════════════════════════════════

async def send_music_control(ws, music_action: dict, tts_callback=None):
    """发送音乐控制消息，如果 play 时携带 query 则先搜索歌曲。

    tts_callback: async (ws, text, path) — TTS 播报回调，避免循环导入
    """
    from playlist_service import get_all_songs, search_local, get_playlist, list_playlists

    action = music_action.get("action", "play")
    query = music_action.get("query", "")

    # ── 切歌/上一首：直接由前端队列处理 ──
    if action in ("next", "prev"):
        await ws.send_json({"type": "music_control", "action": action})
        if tts_callback:
            asyncio.create_task(tts_callback(ws, "好的", "music"))
        return

    # ── 歌单播放 ──
    playlist_name = music_action.get("playlist", "")
    if not playlist_name and not query:
        try:
            all_playlists = list_playlists()
            if all_playlists:
                playlist_name = list(all_playlists.keys())[0]
                logger.info(f"无歌单/无搜索 → 默认歌单: '{playlist_name}'")
        except Exception:
            pass
    if not playlist_name and query:
        try:
            all_playlists = list_playlists()
            q_lower = query.strip().lower()
            for pname in all_playlists:
                if q_lower == pname.lower() or pname.lower() in q_lower:
                    playlist_name = pname
                    logger.info(f"歌单自动匹配: query='{query}' → playlist='{playlist_name}'")
                    break
        except Exception:
            pass

    if playlist_name:
        songs = get_playlist(playlist_name)
        if songs:
            import random as _random
            shuffled = list(songs)
            _random.shuffle(shuffled)

            song_info_list = []
            for s in shuffled:
                song_info_list.append({
                    "song_id": f"playlist_{playlist_name}_{s['filename']}",
                    "song_name": s.get("song_name", s.get("filename", "")),
                    "singers": s.get("artist", ""),
                    "album": "",
                    "source": "playlist",
                    "duration": "",
                    "duration_s": 0,
                    "cover_url": "",
                    "download_url": s["url"],
                    "ext": "mp3",
                    "file_size": "",
                    "file_size_bytes": 0,
                    "quality": "",
                    "lyric": "",
                })

            first = song_info_list[0]
            await ws.send_json({
                "type": "music_control",
                "action": "play",
                "playlist_name": playlist_name,
                "song_id": first["song_id"],
                "song_name": first["song_name"],
                "singers": first["singers"],
                "album": first["album"],
                "source": first["source"],
                "duration": first["duration"],
                "duration_s": first["duration_s"],
                "cover_url": first["cover_url"],
                "download_url": first["download_url"],
                "ext": first["ext"],
                "songs": song_info_list,
            })

            announce_text = f"好的，为你播放歌单「{playlist_name}」"
            if tts_callback:
                asyncio.create_task(tts_callback(ws, announce_text, "music"))
            logger.info(f"歌单播放: '{playlist_name}' ({len(song_info_list)} 首, 已随机打乱)")
            return
        else:
            logger.warning(f"歌单 '{playlist_name}' 未找到，尝试关键词意图回退")

            all_playlists = list_playlists()
            if all_playlists:
                playlist_names = list(all_playlists.keys())
                KEYWORD_MAP: list[tuple[list[str], str]] = [
                    (["学习", "专注", "看书", "读书", "写作业", "备考", "焦虑", "紧张",
                      "助眠", "睡觉", "失眠", "休息", "冥想", "放松",
                      "轻音乐", "轻音", "纯音乐", "纯音", "背景", "安静", "环境",
                      "钢琴", "古筝", "吉他", "琵琶", "二胡", "小提琴", "器乐"], "轻音乐"),
                    (["兴奋", "运动", "跑步", "健身", "锻炼", "燃", "嗨", "劲爆", "摇滚", "节奏"], "运动"),
                    (["日常", "随便", "来点", "放歌", "想听", "播放", "收藏", "流行"], "收藏"),
                ]

                search_text = f"{playlist_name} {query}".strip().lower()

                matched_playlist = None
                for keywords, target_playlist in KEYWORD_MAP:
                    if target_playlist not in playlist_names:
                        continue
                    if any(kw in search_text for kw in keywords):
                        matched_playlist = target_playlist
                        logger.info(f"歌单关键词回退命中: '{search_text}' → '{matched_playlist}'")
                        break

                if not matched_playlist:
                    matched_playlist = playlist_names[0]
                    logger.info(f"歌单无关键词匹配，默认选第一个: '{matched_playlist}'")

                songs = get_playlist(matched_playlist)
                if songs:
                    import random as _random2
                    shuffled = list(songs)
                    _random2.shuffle(shuffled)

                    song_info_list = []
                    for s in shuffled:
                        song_info_list.append({
                            "song_id": f"playlist_{matched_playlist}_{s['filename']}",
                            "song_name": s.get("song_name", s.get("filename", "")),
                            "singers": s.get("artist", ""),
                            "album": "",
                            "source": "playlist",
                            "duration": "",
                            "duration_s": 0,
                            "cover_url": "",
                            "download_url": s["url"],
                            "ext": "mp3",
                            "file_size": "",
                            "file_size_bytes": 0,
                            "quality": "",
                            "lyric": "",
                        })

                    first = song_info_list[0]
                    await ws.send_json({
                        "type": "music_control",
                        "action": "play",
                        "playlist_name": matched_playlist,
                        "song_id": first["song_id"],
                        "song_name": first["song_name"],
                        "singers": first["singers"],
                        "album": first["album"],
                        "source": first["source"],
                        "duration": first["duration"],
                        "duration_s": first["duration_s"],
                        "cover_url": first["cover_url"],
                        "download_url": first["download_url"],
                        "ext": first["ext"],
                        "songs": song_info_list,
                    })

                    announce_text = f"好的，为你播放歌单「{matched_playlist}」"
                    if tts_callback:
                        asyncio.create_task(tts_callback(ws, announce_text, "music"))
                    logger.info(f"歌单回退播放: '{matched_playlist}' ({len(song_info_list)} 首, 关键词映射)")
                    return
                else:
                    logger.warning(f"回退歌单 '{matched_playlist}' 也无歌曲，继续搜索流水线")

    if action == "play" and query:
        clean_query = clean_music_query(query)
        logger.info(f"搜索歌曲: '{query}' (清理后: '{clean_query}')")

        await ws.send_json({
            "type": "music_search_status",
            "status": "searching",
            "query": clean_query or query,
        })

        # ── 记忆偏好联动：短 query（可能只是歌手名）查记忆扩展 ──
        if clean_query and len(clean_query) < 6:
            try:
                from services.memory_engine import get_memory
                memory = get_memory()
                memories = memory.get_all()
                for mem in memories:
                    if mem.get("category") == "preference" and clean_query in str(mem.get("value", "")):
                        expanded = f"{clean_query} 热门歌曲"
                        logger.info(f"记忆偏好联动: '{clean_query}' → '{expanded}'")
                        clean_query = expanded
                        break
            except Exception:
                pass

        # ── 泛化请求（无具体歌名）→ 随机播放本地歌单
        if not clean_query:
            all_local = get_all_songs()
            if all_local:
                import random as _random
                picked = _random.choice(all_local)
                await _announce_and_play(ws, _build_local_song(picked), [_build_local_song(s) for s in all_local], tts_callback)
                logger.info(f"泛化播放，随机选择: {picked['song_name']} - {picked['artist']}")
                return
            logger.info("泛化播放请求但本地歌单为空，使用默认纯音乐")
            await ws.send_json({
                "type": "music_control",
                "action": "play",
                "song_name": "专注纯音乐",
                "download_url": "/music/running-up-that-hill.mp3",
                "source": "local",
            })
            return

        # ── 五级搜索流水线（本地 → 网易云 → Jamendo → Pixabay → 163元数据） ──
        # 1. 本地歌单搜索（含 difflib 模糊匹配）
        local_hit = search_local(clean_query) if clean_query else None
        if local_hit:
            all_local = get_all_songs()
            await _announce_and_play(ws, _build_local_song(local_hit), [_build_local_song(s) for s in all_local], tts_callback)
            logger.info(f"本地命中: {local_hit['song_name']} - {local_hit['artist']} (队列 {len(all_local)} 首)")
            return

        # 2. 网易云音乐（主力在线源，支持中文流行歌曲播放）
        from services.netease_cloud_api import get_netease_api
        netease_api = get_netease_api()
        if await netease_api.check_available():
            netease_songs = await netease_api.search_songs(clean_query)
            if netease_songs:
                netease_songs = await _batch_fetch_netease_urls(netease_songs, count=5)
                first = netease_songs[0]
                if first.get("download_url"):
                    await _announce_and_play(ws, first, netease_songs, tts_callback)
                    logger.info(f"网易云命中: {first['song_name']} - {first['singers']} (队列 {len(netease_songs)} 首)")
                    return
                else:
                    logger.info(f"网易云搜到但无播放链接: {first['song_name']} - {first['singers']}，回退下一级")
        else:
            logger.info("网易云 API 不可用，跳过")

        # 3. Jamendo API（CC 授权独立音乐，有真实可播放 MP3 URL）
        jamendo_songs = await _search_jamendo(clean_query)
        if jamendo_songs:
            first = jamendo_songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first, jamendo_songs, tts_callback)
                logger.info(f"Jamendo 命中: {first.get('song_name', '')} - {first.get('singers', '')}")
                return

        # 3a. 轻音乐/纯音乐/学习 mood 搜索 → Jamendo 用英文标签
        eng_tags = _mood_to_english(clean_query)
        if eng_tags:
            logger.info(f"Mood 搜索: '{clean_query}' → Jamendo English tags: '{eng_tags}'")
            jamendo_mood = await _search_jamendo(eng_tags)
            if jamendo_mood:
                first = jamendo_mood[0]
                if first.get("download_url"):
                    await _announce_and_play(ws, first, jamendo_mood, tts_callback)
                    logger.info(f"Jamendo mood 命中: {first.get('song_name', '')} - {first.get('singers', '')}")
                    return

        # 4. Pixabay Music（免费可商用，有真实 MP3 URL）
        from services.pixabay_music import search_pixabay_music
        pixabay_songs = await search_pixabay_music(clean_query)
        if pixabay_songs:
            first = pixabay_songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first, pixabay_songs, tts_callback)
                logger.info(f"Pixabay 命中: {first.get('song_name', '')}")
                return

        # 5. 163 在线搜索（仅元数据，无可靠播放 URL，仅作最后参考）
        songs = await search_songs(clean_query)
        if songs:
            first = songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first, songs, tts_callback)
                logger.info(f"163 播放: {first.get('song_name', '')} by {first.get('singers', '')}")
                return
            logger.info(f"163 结果无播放链接: {first.get('song_name', '')}")

        # ── 版权拦截：中文 query + 所有源都未命中 → 告知用户
        if _is_chinese_query(clean_query):
            if _mood_to_english(clean_query):
                logger.info(f"Mood 搜索所有源未命中，播放默认纯音乐: '{clean_query}'")
                await ws.send_json({
                    "type": "music_control",
                    "action": "play",
                    "song_name": f"纯音乐 · {clean_query[:20]}",
                    "download_url": "/music/running-up-that-hill.mp3",
                    "source": "local",
                })
                return
            msg = "抱歉，我不能为你播放有版权的歌曲。你可以把 mp3 文件放到 music 目录，我就能播放了"
            await ws.send_json({
                "type": "music_search_status",
                "status": "copyright_blocked",
                "message": msg,
                "query": clean_query,
            })
            if tts_callback:
                asyncio.create_task(tts_callback(ws, msg, "music"))
            logger.info(f"版权拦截: '{clean_query}'（中文商业版权歌曲，所有源均未命中）")
            return

        # ── 回退：非中文 → 随机本地 + 告知用户
        all_local = get_all_songs()
        if all_local:
            import random as _random
            picked = _random.choice(all_local)
            fallback_msg = f"抱歉，我没找到「{query}」的可播放版本，为你随机播放一首本地歌曲吧"
            await ws.send_json({"type": "chat_error", "message": fallback_msg})
            await _announce_and_play(ws, _build_local_song(picked), [_build_local_song(s) for s in all_local], tts_callback)
            logger.info(f"回退本地随机: {picked['song_name']} - {picked['artist']}")
        else:
            await ws.send_json({
                "type": "music_search_status",
                "status": "not_found",
                "message": f"未找到歌曲「{query}」，本地歌单也为空，请先添加音乐文件",
                "query": clean_query,
            })

    elif action == "play" and not query:
        # 随机播放：优先本地歌单 → 网易云热门推荐 → Pixabay → 报错
        all_local = get_all_songs()
        if all_local:
            import random as _random
            picked = _random.choice(all_local)
            await _announce_and_play(ws, _build_local_song(picked), [_build_local_song(s) for s in all_local], tts_callback)
            logger.info(f"随机播放本地: {picked['song_name']} - {picked['artist']}")
        else:
            from services.netease_cloud_api import get_netease_api
            netease_api = get_netease_api()
            if await netease_api.check_available():
                netease_songs = await netease_api.search_songs("热门歌曲", limit=10)
                if netease_songs:
                    netease_songs = await _batch_fetch_netease_urls(netease_songs, count=5)
                    first = netease_songs[0]
                    if first.get("download_url"):
                        await _announce_and_play(ws, first, netease_songs, tts_callback)
                        logger.info(f"随机播放网易云: {first['song_name']} - {first['singers']}")
                        return
                    else:
                        logger.info("网易云随机播放无可用 URL，继续回退")
            from services.pixabay_music import search_pixabay_music
            pixabay_songs = await search_pixabay_music("relaxing music")
            if pixabay_songs:
                first = pixabay_songs[0]
                if first.get("download_url"):
                    await _announce_and_play(ws, first, pixabay_songs, tts_callback)
                    logger.info(f"随机播放 Pixabay: {first.get('song_name', '')}")
                    return
            logger.info("随机播放失败：所有源都无可用歌曲")
            await ws.send_json({
                "type": "chat_error",
                "message": "本地歌单为空，在线曲库也未找到歌曲，请先添加音乐文件",
            })
    else:
        # pause / resume / stop — 直接转发给前端
        await ws.send_json({"type": "music_control", "action": action})
        if action == "pause":
            if tts_callback:
                asyncio.create_task(tts_callback(ws, "已暂停", "music"))
        elif action == "stop":
            if tts_callback:
                asyncio.create_task(tts_callback(ws, "已停止", "music"))


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
