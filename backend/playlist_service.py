"""
本地歌单服务 —— 自动扫描 music/ 目录，本地优先匹配
本地未命中才 fallback 到 musicdl 在线搜索
"""
import difflib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# 前端静态文件目录（MP3 文件）
MUSIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),  # backend/
    "..", "frontend", "public", "music",
)

# 本地歌单索引缓存
_local_playlist: list[dict] = []


def _parse_filename(filename: str) -> dict:
    """
    从文件名解析歌曲信息。
    支持格式：
      - 歌手-歌名.mp3
      - 歌手 - 歌名 [xxx].mp3
      - 歌名-歌手.mp3
      - 歌名.mp3
    """
    name = os.path.splitext(filename)[0]
    # 去掉 [mqms]、[mqms2] 等后缀标记
    name = re.sub(r'\s*\[.*?\]', '', name).strip()
    url = f"/music/{filename}"

    # 尝试提取歌手和歌名
    # 格式: "歌手-歌名" 或 "歌手 - 歌名"
    parts = re.split(r'\s*[-–—]\s*', name, maxsplit=1)
    if len(parts) == 2:
        artist, song = parts[0].strip(), parts[1].strip()
    else:
        song = name.strip()
        artist = ""

    # 构建搜索关键词：同时保留原文件名、歌手+歌名、纯歌名
    search_terms = set()
    search_terms.add(name.lower())
    if song:
        search_terms.add(song.lower())
    if artist and song:
        search_terms.add(f"{artist} {song}".lower())

    return {
        "song_name": song,
        "artist": artist,
        "filename": filename,
        "url": url,
        "search_terms": list(search_terms),
    }


def refresh_playlist():
    """扫描 music/ 目录，刷新本地歌单索引"""
    global _local_playlist
    _local_playlist = []

    if not os.path.isdir(MUSIC_DIR):
        logger.warning(f"音乐目录不存在: {MUSIC_DIR}")
        return

    for f in sorted(os.listdir(MUSIC_DIR)):
        if not f.endswith((".mp3", ".wav", ".flac", ".ogg")):
            continue
        info = _parse_filename(f)
        _local_playlist.append(info)

    logger.info(f"本地歌单已刷新: {len(_local_playlist)} 首歌曲 ({MUSIC_DIR})")


def search_local(query: str, fuzzy_threshold: float = 0.6) -> dict | None:
    """
    在本地歌单中搜索（含 difflib 模糊匹配）。
    Returns: {"song_name":..., "artist":..., "url":..., "filename":...} 或 None
    """
    if not _local_playlist:
        refresh_playlist()

    if not _local_playlist:
        return None

    q = query.lower().strip()
    # 1. 精确匹配 search_terms
    for song in _local_playlist:
        if q in song["search_terms"] or any(q == t for t in song["search_terms"]):
            logger.info(f"本地歌单命中（精确）: {song['song_name']} - {song['artist']}")
            return song

    # 2. 部分匹配（查询词被包含在歌名或歌手名中）
    for song in _local_playlist:
        if q in song["song_name"].lower() or q in song["artist"].lower():
            logger.info(f"本地歌单命中（模糊）: {song['song_name']} - {song['artist']}")
            return song

    # 3. 查询词包含歌名或歌手名
    for song in _local_playlist:
        if song["song_name"].lower() in q or song["artist"].lower() in q:
            logger.info(f"本地歌单命中（反向）: {song['song_name']} - {song['artist']}")
            return song

    # 4. difflib 模糊匹配（相似度 > fuzzy_threshold）
    best_score = 0.0
    best_song = None
    for song in _local_playlist:
        candidates = [song["song_name"].lower(), song["artist"].lower()]
        candidates.extend(song.get("search_terms", []))
        for c in candidates:
            score = difflib.SequenceMatcher(None, q, c).ratio()
            if score > best_score:
                best_score = score
                best_song = song
    if best_song and best_score >= fuzzy_threshold:
        logger.info(f"本地歌单命中（difflib: {best_score:.2f}）: {best_song['song_name']} - {best_song['artist']}")
        return best_song

    return None


def get_all_songs() -> list[dict]:
    """获取完整本地歌单"""
    if not _local_playlist:
        refresh_playlist()
    # 返回不包含 search_terms
    return [
        {"song_name": s["song_name"], "artist": s["artist"],
         "filename": s["filename"], "url": s["url"]}
        for s in _local_playlist
    ]
