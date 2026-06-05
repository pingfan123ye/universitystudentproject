"""
智能音箱_无实物 —— 后端服务入口 (v0.2.0)
FastAPI + WebSocket 实现 AI 语音交互中枢（三道路由分发）
"""
import asyncio
import base64
import json
import logging
import os
import re
import socket
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from services.intent_router import classify
from services.llm_service import generate_stream, check_model_available, check_deepseek_available, DEFAULT_MODEL
from services.xiaoai_service import execute as xiaoai_execute
from services.reasonix_executor import execute as reasonix_execute, is_reasonix_available, get_pending_manager
from services.cache_engine import get_cache
from services.memory_engine import get_memory
from services.whisper_stt import transcribe_audio_base64 as stt_transcribe, is_available as stt_available
from services.stt_corrector import correct as stt_correct
from services.tts_service import text_to_speech_base64
from services.context_engine import get_context_engine
from services.search_service import search_to_context
from services.safety_filter import assess_risk, format_confirm_message, requires_confirmation
from services.llm_service import strip_search_tags, parse_actions
from services.engine_config import get_config
from models.virtual_home import VirtualHome, get_virtual_time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ===== 强制清理旧进程 =====
PORT = 8000


def _kill_old_process():
    """杀掉占用目标端口的旧进程，确保新代码能启动"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=10
            )
            killed = False
            for line in result.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    print(f"[启动] 发现旧进程 PID={pid} 占用端口 {PORT}，正在终止...")
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed = True
            if killed:
                import time
                time.sleep(1)
                print("[启动] 旧进程已清理，端口已释放")
            else:
                print(f"[启动] 端口 {PORT} 空闲")
        else:
            # Linux/macOS: 使用 fuser/lsof
            result = subprocess.run(
                ["lsof", "-ti", f":{PORT}"], capture_output=True, text=True, timeout=10
            )
            pids = result.stdout.strip().splitlines()
            if pids and pids[0]:
                for pid in pids:
                    print(f"[启动] 发现旧进程 PID={pid} 占用端口 {PORT}，正在终止...")
                    subprocess.run(["kill", "-9", pid], capture_output=True)
                import time
                time.sleep(1)
                print("[启动] 旧进程已清理，端口已释放")
            else:
                print(f"[启动] 端口 {PORT} 空闲")
    except Exception as e:
        print(f"[启动] 清理旧进程时出错: {e}")


_kill_old_process()

# ===== 流式 STT 累积缓冲区 =====
_stt_buffer: str = ""  # 分段音频转写结果的累积字符串

# ===== 非阻塞 TTS 辅助 =====
async def _maybe_tts(ws: WebSocket, reply: str, path: str):
    """非阻塞发送 TTS 音频，失败时通知前端降级 speechSynthesis"""
    if not reply or not reply.strip():
        return
    # 清洗 Markdown 格式字符，避免 TTS 朗读星号/井号等
    reply = _clean_for_tts(reply)
    if not reply:
        return
    try:
        audio_b64 = await text_to_speech_base64(reply)
        if audio_b64:
            await ws.send_json({
                "type": "tts_audio",
                "audio": audio_b64,
                "text": reply[:120],
                "path": path,
            })
        else:
            await ws.send_json({
                "type": "tts_failed",
                "text": reply[:200],
                "reason": "TTS 合成失败，请前端降级",
            })
    except Exception:
        await ws.send_json({
            "type": "tts_failed",
            "text": reply[:200],
            "reason": "TTS 异常",
        })



def _clean_song_query(text: str) -> str:
    """清理搜索词：移除指令词、唤醒词、礼貌前缀、语气词，只保留歌名关键词"""
    import re as _re
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
    # 6. 如果是纯泛化请求，返回空串让上游随机播放
    if _re.match(r"^(歌曲|歌|音乐|音樂|一首歌|几首歌|来首歌|随便|随便来|随便听|随便听歌|来点音乐|什么歌|啥歌|点儿歌|点歌|听歌|想听歌|什么都行|啥都行|都可以|随便什么|什么都可|随意|随机|任意)\s*$", cleaned):
        return ""
    # 7. 去掉尾部的语气词
    cleaned = _re.sub(r"[啊哦嗯呀吧啦呗么]$", "", cleaned)
    # 8. 去掉口语功能词（永远不可能是歌名/歌手名的词）
    cleaned = _re.sub(r'\b(好的|进行|一下|那个|这个|选首|选个|来帮我|帮我选|选手|选手给|随机|随机多|多放|多|我让|我讓|让你|讓你|随便|隨便|随意|隨意|字幕)\b', ' ', cleaned)
    cleaned = _re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


# ===== 音乐控制辅助 =====
def _is_chinese_query(query: str) -> bool:
    """检测 query 是否包含中文字符（用于版权拦截判断）"""
    return bool(re.search(r'[一-鿿]', query))


def _clean_for_tts(text: str) -> str:
    """清洗 Markdown / 结构化格式字符，让 TTS 只朗读纯文本"""
    import re as _re
    # 1. 去掉代码块（```...```）
    text = _re.sub(r'```[\s\S]*?```', '', text)
    # 2. 去掉行内代码 (`code`)
    text = _re.sub(r'`([^`]+)`', r'\1', text)
    # 3. 去掉粗体标记（**text**, __text__）
    text = _re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = _re.sub(r'__([^_]+)__', r'\1', text)
    # 4. 去掉斜体标记（*text* / _text_），注意不误伤单独出现的 * 或 _
    text = _re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)
    text = _re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'\1', text)
    # 5. 去掉标题标记（# ## ### 等，只在行首匹配）
    text = _re.sub(r'^#{1,6}\s+', '', text, flags=_re.MULTILINE)
    # 6. 去掉无序列表标记（- * + 开头）
    text = _re.sub(r'^[\-\*\+]\s+', '', text, flags=_re.MULTILINE)
    # 7. 去掉有序列表标记（1. 2. 等）
    text = _re.sub(r'^\d+\.\s+', '', text, flags=_re.MULTILINE)
    # 8. 去掉删除线（~~text~~）
    text = _re.sub(r'~~([^~]+)~~', r'\1', text)
    # 9. 去掉多余空白行
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _mood_to_english(query: str) -> str:
    """中文情绪/场景词 → Jamendo 英文标签（轻音乐/纯音乐等 Jamendo 中文无结果）"""
    if not query:
        return ""
    q = query.lower()
    # 先精确匹配整段
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


async def _announce_and_play(ws, song: dict, all_local_songs: list[dict] | None = None):
    """发送播放指令 + TTS 语音播报歌名。
    当 all_local_songs 提供时，将其打包为完整队列发给前端（当前歌曲排第一，其余随机），
    这样前端有完整队列，切歌/上一首可以直接在队列中推进。"""
    name = song.get("song_name", "")
    singer = song.get("singers", "")

    # 构建歌曲队列（当前歌曲 + 其余本地歌曲随机排列）
    if all_local_songs:
        import random as _random
        # 将当前命中的歌曲排第一，其余随机打乱（用 song_id 去重）
        current_filename = song.get("song_id", "").replace("local_", "")
        others = [_build_local_song(s) for s in all_local_songs
                  if s.get("filename") != current_filename]
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
    # TTS 播报：告诉用户正在播放什么
    if singer:
        announce_text = f"好的，为你播放{singer}的{name}"
    else:
        announce_text = f"好的，为你播放{name}"
    asyncio.create_task(_maybe_tts(ws, announce_text, "music"))
    logger.info(f"播放: {name} - {singer}" + (f" (队列 {len(songs_payload)} 首)" if all_local_songs else ""))


def _build_local_song(picked: dict) -> dict:
    return {
        "song_id": f"local_{picked['filename']}",
        "song_name": picked["song_name"], "singers": picked["artist"],
        "album": "", "source": "local", "duration": "", "duration_s": 0,
        "cover_url": "", "download_url": picked["url"], "ext": "mp3",
        "file_size": "", "file_size_bytes": 0, "quality": "", "lyric": "",
    }


async def _send_music_control(ws, music_action: dict):
    """发送音乐控制消息，如果 play 时携带 query 则先搜索歌曲"""
    from services.music_service import MusicService, _search_jamendo
    from playlist_service import get_all_songs, search_local, get_playlist, list_playlists

    action = music_action.get("action", "play")
    query = music_action.get("query", "")

    # ── 切歌/上一首：直接由前端队列处理 ──
    if action in ("next", "prev"):
        # 前端已有歌曲队列（播放器/歌单），直接发指令让前端推进。
        # 如果前端没歌曲，next() 无害（队列空则不操作/播默认曲目）
        await ws.send_json({"type": "music_control", "action": action})
        asyncio.create_task(_maybe_tts(ws, "好的", "music"))
        return

    # ── 歌单播放 ──
    playlist_name = music_action.get("playlist", "")
    # 无歌单 + 无搜索词 → 默认第一个可用歌单（避免随机单曲体验差）
    if not playlist_name and not query:
        try:
            all_playlists = list_playlists()
            if all_playlists:
                playlist_name = list(all_playlists.keys())[0]
                logger.info(f"无歌单/无搜索 → 默认歌单: '{playlist_name}'")
        except Exception:
            pass
    # 如果没显式指定 playlist 但 query 匹配某个歌单名，自动回退
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
            shuffled = list(songs)  # 复制
            _random.shuffle(shuffled)

            # 构建完整 SongInfo 对象
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

            # TTS 播报
            announce_text = f"好的，为你播放歌单「{playlist_name}」"
            asyncio.create_task(_maybe_tts(ws, announce_text, "music"))
            logger.info(f"歌单播放: '{playlist_name}' ({len(song_info_list)} 首, 已随机打乱)")
            return
        else:
            logger.warning(f"歌单 '{playlist_name}' 未找到，尝试关键词意图回退")

            # ── 歌单名匹配失败 → 关键词意图映射回退 ──
            # LLM 可能编造歌单名（如"轻松学习曲目"），从其中提取关键词来匹配真实歌单
            all_playlists = list_playlists()
            if all_playlists:
                playlist_names = list(all_playlists.keys())
                # 关键词 → 偏好歌单名 映射（优先级从高到低）
                KEYWORD_MAP: list[tuple[list[str], str]] = [
                    (["学习", "专注", "看书", "读书", "写作业", "备考", "焦虑", "紧张",
                      "助眠", "睡觉", "失眠", "休息", "冥想", "放松",
                      "轻音乐", "轻音", "纯音乐", "纯音", "背景", "安静", "环境",
                      "钢琴", "古筝", "吉他", "琵琶", "二胡", "小提琴", "器乐"], "轻音乐"),
                    (["兴奋", "运动", "跑步", "健身", "锻炼", "燃", "嗨", "劲爆", "摇滚", "节奏"], "运动"),
                    (["日常", "随便", "来点", "放歌", "想听", "播放", "收藏", "流行"], "收藏"),
                ]

                # 从 LLM 输出的 playlist_name + query 中提取关键词
                search_text = f"{playlist_name} {query}".strip().lower()

                matched_playlist = None
                for keywords, target_playlist in KEYWORD_MAP:
                    if target_playlist not in playlist_names:
                        continue  # 目标歌单不存在则跳过
                    if any(kw in search_text for kw in keywords):
                        matched_playlist = target_playlist
                        logger.info(f"歌单关键词回退命中: '{search_text}' → '{matched_playlist}'")
                        break

                # 无关键词匹配 → 默认第一个歌单
                if not matched_playlist:
                    matched_playlist = playlist_names[0]
                    logger.info(f"歌单无关键词匹配，默认选第一个: '{matched_playlist}'")

                # 用匹配到的歌单再试一次
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
                    asyncio.create_task(_maybe_tts(ws, announce_text, "music"))
                    logger.info(f"歌单回退播放: '{matched_playlist}' ({len(song_info_list)} 首, 关键词映射)")
                    return
                else:
                    logger.warning(f"回退歌单 '{matched_playlist}' 也无歌曲，继续搜索流水线")

    if action == "play" and query:
        clean_query = _clean_song_query(query)
        logger.info(f"搜索歌曲: '{query}' (清理后: '{clean_query}')")

        # 发送搜索状态给前端
        await ws.send_json({
            "type": "music_search_status",
            "status": "searching",
            "query": clean_query or query,
        })

        # ── 记忆偏好联动：短 query（可能只是歌手名）查记忆扩展 ──
        if clean_query and len(clean_query) < 6:
            try:
                memory = get_memory()
                memories = memory.get_all()
                for mem in memories:
                    if mem.get("category") == "preference" and clean_query in str(mem.get("value", "")):
                        expanded = f"{clean_query} 热门歌曲"
                        logger.info(f"记忆偏好联动: '{clean_query}' → '{expanded}'")
                        clean_query = expanded
                        break
            except Exception:
                pass  # 记忆引擎失败不影响播放

        # 泛化请求（无具体歌名）→ 随机播放本地歌单
        if not clean_query:
            all_local = get_all_songs()
            if all_local:
                import random as _random
                picked = _random.choice(all_local)
                await _announce_and_play(ws, _build_local_song(picked), all_local)
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

        # ── 四级搜索流水线 ──
        # 1. 本地歌单搜索（含 difflib 模糊匹配）
        local_hit = search_local(clean_query) if clean_query else None
        if local_hit:
            all_local = get_all_songs()
            await _announce_and_play(ws, _build_local_song(local_hit), all_local)
            logger.info(f"本地命中: {local_hit['song_name']} - {local_hit['artist']} (队列 {len(all_local)} 首)")
            return

        # 2. Jamendo API（CC 授权独立音乐，有真实可播放 MP3 URL）
        jamendo_songs = await _search_jamendo(clean_query)
        if jamendo_songs:
            first = jamendo_songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first)
                logger.info(f"Jamendo 命中: {first.get('song_name', '')} - {first.get('singers', '')}")
                return

        # 2a. 轻音乐/纯音乐/学习 mood 搜索 → Jamendo 用英文标签（中文曲库少）
        eng_tags = _mood_to_english(clean_query)
        if eng_tags:
            logger.info(f"Mood 搜索: '{clean_query}' → Jamendo English tags: '{eng_tags}'")
            jamendo_mood = await _search_jamendo(eng_tags)
            if jamendo_mood:
                first = jamendo_mood[0]
                if first.get("download_url"):
                    await _announce_and_play(ws, first)
                    logger.info(f"Jamendo mood 命中: {first.get('song_name', '')} - {first.get('singers', '')}")
                    return

        # 3. Pixabay Music（免费可商用，有真实 MP3 URL）
        from services.pixabay_music import search_pixabay_music
        pixabay_songs = await search_pixabay_music(clean_query)
        if pixabay_songs:
            first = pixabay_songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first)
                logger.info(f"Pixabay 命中: {first.get('song_name', '')}")
                return

        # 4. 163 在线搜索（仅元数据，预览 URL 不可靠，仅作参考）
        ms = MusicService()
        songs = await ms.search_songs(clean_query)
        if songs:

            first = songs[0]
            if first.get("download_url"):
                await _announce_and_play(ws, first)
                logger.info(f"163 播放: {first.get('song_name', '')} by {first.get('singers', '')}")
                return
            logger.info(f"163 结果无播放链接: {first.get('song_name', '')}")

        # ── 5. 版权拦截：中文 query + 所有源都未命中 → 告知用户 ──
        if _is_chinese_query(clean_query):
            # 如果是轻音乐/纯音乐/学习 mood 请求，放默认纯音乐代替报错
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
            asyncio.create_task(_maybe_tts(ws, msg, "music"))
            logger.info(f"版权拦截: '{clean_query}'（中文商业版权歌曲，所有源均未命中）")
            return

        # ── 6. 回退：非中文 → 随机本地 + 告知用户 ──
        all_local = get_all_songs()
        if all_local:
            import random as _random
            picked = _random.choice(all_local)
            fallback_msg = f"抱歉，我没找到「{query}」的可播放版本，为你随机播放一首本地歌曲吧"
            await ws.send_json({"type": "chat_error", "message": fallback_msg})
            await _announce_and_play(ws, _build_local_song(picked), all_local)
            logger.info(f"回退本地随机: {picked['song_name']} - {picked['artist']}")
        else:
            await ws.send_json({
                "type": "music_search_status",
                "status": "not_found",
                "message": f"未找到歌曲「{query}」，本地歌单也为空，请先添加音乐文件",
                "query": clean_query,
            })

    elif action == "play" and not query:
        # 随机播放：优先本地歌单随机选一首 → Pixabay 随机 → 报错
        all_local = get_all_songs()
        if all_local:
            import random as _random
            picked = _random.choice(all_local)
            await _announce_and_play(ws, _build_local_song(picked), all_local)
            logger.info(f"随机播放本地: {picked['song_name']} - {picked['artist']}")
        else:
            # 本地歌单为空，尝试 Pixabay 随机抓取
            from services.pixabay_music import search_pixabay_music
            pixabay_songs = await search_pixabay_music("relaxing music")
            if pixabay_songs:
                first = pixabay_songs[0]
                if first.get("download_url"):
                    await _announce_and_play(ws, first)
                    logger.info(f"随机播放 Pixabay: {first.get('song_name', '')}")
                    return
            logger.info("随机播放失败：本地和 Pixabay 都无歌曲")
            await ws.send_json({
                "type": "chat_error",
                "message": "本地歌单为空，在线曲库也未找到歌曲，请先添加音乐文件",
            })
    else:
        # pause / resume / stop — 直接转发给前端
        await ws.send_json({"type": "music_control", "action": action})
        if action == "pause":
            asyncio.create_task(_maybe_tts(ws, "已暂停", "music"))
        elif action == "stop":
            asyncio.create_task(_maybe_tts(ws, "已停止", "music"))
        # resume 不 TTS：音乐恢复播放本身就是最直接的反馈


# ===== 全局虚拟家庭实例 =====
virtual_home = VirtualHome()
_pending_safety_cmd: str | None = None  # 待安全确认的命令
APP_VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = f"""
========================================
  智能音箱后端 v{APP_VERSION}
  三道路由: 设备 | 大模型 | Reasonix
========================================
"""
    print(banner)
    logger.info(f"后端 v{APP_VERSION} 启动中")
    logger.info("正在检查 Ollama 模型...")
    if await check_model_available():
        logger.info(f"模型 {DEFAULT_MODEL} 已就绪")
    else:
        logger.warning(f"模型 {DEFAULT_MODEL} 未找到")
    logger.info(f"Reasonix CLI 可用: {is_reasonix_available()}")
    logger.info("预加载 Whisper STT 模型（后台下载）...")
    from services.whisper_stt import preload_model
    await preload_model()
    logger.info("三道路由分发已启用")
    yield


app = FastAPI(title="智能音箱_无实物 API", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health_check():
    model_ok = await check_model_available()
    deepseek_ok = await check_deepseek_available()
    from services.intent_router import classify
    d = classify("测试路由")
    return JSONResponse({
        "status": "ok",
        "version": APP_VERSION,
        "model_available": model_ok,
        "model": DEFAULT_MODEL,
        "deepseek_available": deepseek_ok,
        "reasonix_available": is_reasonix_available(),
        "router": "xiaoai | info_query | mixed | reasonix | llm",
        "last_route": d.path,
    })


@app.get("/api/proxy/music")
async def proxy_music(url: str = Query(..., description="要代理的音乐URL")):
    """代理第三方音乐资源，解决跨域播放问题"""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            content_type = resp.headers.get("content-type", "audio/mpeg")
            content_length = resp.headers.get("content-length", "")
            headers = {"Accept-Ranges": "bytes"}
            if content_length and content_length.strip():
                headers["Content-Length"] = content_length
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=200,
                media_type=content_type,
                headers=headers,
            )
    except Exception as e:
        logger.warning(f"音乐代理失败: {e}")
        return JSONResponse({"error": "代理失败"}, status_code=502)


# ── 歌单 API ──
@app.get("/api/playlists")
async def api_list_playlists():
    """列出所有歌单及其歌曲数"""
    from playlist_service import list_playlists
    return {"playlists": list_playlists()}


@app.get("/api/playlists/{name}")
async def api_get_playlist(name: str):
    """获取指定歌单的歌曲列表"""
    from playlist_service import get_playlist
    songs = get_playlist(name)
    return {"name": name, "songs": songs or []}


@app.post("/api/playlists/{name}/refresh")
async def api_refresh_playlist(name: str):
    """强制刷新歌单索引"""
    from playlist_service import refresh_playlists
    refresh_playlists()
    return {"ok": True}


@app.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket 端点 —— 三道路由分发中枢

    接收: {"type": "chat"|"ping"|"get_devices", "text": "用户输入"}
    发送:
        {"type": "route", "path": "...", "reason": "..."}       — 路由决策
        {"type": "token", "text": "..."}                        — 流式回复
        {"type": "device_state", "devices": {...}}              — 设备状态
        {"type": "done", "path": "...", "reply": "..."}         — 完成
        {"type": "error", "error": "..."}                       — 错误
    """
    await ws.accept()
    global _stt_buffer
    logger.info("WebSocket 客户端已连接")

    # 发送初始设备状态
    await ws.send_json({
        "type": "device_state",
        "devices": virtual_home.get_all_states(),
    })
    # 发送初始时间状态
    vt = get_virtual_time()
    await ws.send_json({
        "type": "time_sync",
        "time": vt.to_dict(),
    })

    # 启动情境引擎
    async def _on_alert(alert: dict):
        """情境引擎触发时推送主动提醒"""
        try:
            await ws.send_json({
                "type": "proactive_alert",
                "alert": alert,
            })
            logger.info(f"主动提醒推送: {alert.get('reason', '')}")
        except Exception:
            pass  # WebSocket 可能已断开

    # 初始化并启动情境引擎
    ce = get_context_engine(
        get_virtual_time=get_virtual_time,
        get_memory_engine=lambda: get_memory(),
        get_virtual_home=lambda: virtual_home,
    )
    ce.set_alert_callback(_on_alert)
    await ce.start(interval_seconds=30)

    # 对话历史（跨轮记忆，最多保留最近 20 条消息 = 10 轮）
    conversation_history: list[dict] = []

    # 安全确认命令（模块级变量声明）
    global _pending_safety_cmd

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "消息格式错误"})
                continue

            msg_type = data.get("type", "")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if msg_type == "get_devices":
                await ws.send_json({
                    "type": "device_state",
                    "devices": virtual_home.get_all_states(),
                })
                continue

            if msg_type == "list_cache":
                entries = get_cache().get_all()
                await ws.send_json({"type": "cache_list", "entries": entries})
                continue

            if msg_type == "delete_cache":
                cache_id = data.get("id", "")
                if cache_id:
                    get_cache().delete(cache_id)
                    await ws.send_json({"type": "cache_deleted", "id": cache_id})
                continue

            if msg_type == "list_memories":
                entries = get_memory().get_all()
                await ws.send_json({"type": "memory_list", "entries": entries})
                continue

            if msg_type == "delete_memory":
                mem_id = data.get("id", 0)
                if mem_id:
                    get_memory().delete(int(mem_id))
                    await ws.send_json({"type": "memory_deleted", "id": mem_id})
                continue

            if msg_type == "clear_memories":
                get_memory().clear_all()
                await ws.send_json({"type": "memory_cleared"})
                continue

            # ===== 时间控制 =====
            if msg_type == "get_time":
                vt = get_virtual_time()
                await ws.send_json({"type": "time_sync", "time": vt.to_dict()})
                continue

            if msg_type == "set_time":
                vt = get_virtual_time()
                hour = data.get("hour", 8)
                minute = data.get("minute", 0)
                vt.set_time(hour, minute)
                await ws.send_json({"type": "time_sync", "time": vt.to_dict()})
                continue

            if msg_type == "set_time_speed":
                vt = get_virtual_time()
                speed = data.get("speed", 1.0)
                vt.set_speed(speed)
                await ws.send_json({"type": "time_sync", "time": vt.to_dict()})
                continue

            if msg_type == "toggle_time_pause":
                vt = get_virtual_time()
                vt.toggle_pause()
                await ws.send_json({"type": "time_sync", "time": vt.to_dict()})
                continue

            if msg_type == "toggle_time_simulation":
                vt = get_virtual_time()
                enabled = data.get("enabled", True)
                vt.enable(enabled)
                await ws.send_json({"type": "time_sync", "time": vt.to_dict()})
                continue

            # ===== 主动提醒控制 =====
            if msg_type == "toggle_suppress_alerts":
                suppressed = data.get("suppressed", True)
                ce.set_suppressed(suppressed)
                await ws.send_json({
                    "type": "alerts_suppressed",
                    "suppressed": suppressed,
                })
                continue

            # ===== 安全确认回复 =====
            if msg_type == "safety_reply":
                accept = data.get("accept", False)
                if accept and _pending_safety_cmd:
                    logger.info(f"安全确认通过，执行: {_pending_safety_cmd[:50]}...")
                    proc = await asyncio.create_subprocess_shell(
                        _pending_safety_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    output = ""
                    if proc.stdout:
                        async for line in proc.stdout:
                            text_line = line.decode("utf-8", errors="replace")
                            output += text_line
                            await ws.send_json({"type": "token", "text": text_line})
                    await ws.send_json({"type": "done", "path": "safety", "reply": output.strip()})
                else:
                    await ws.send_json({
                        "type": "done", "path": "safety",
                        "reply": "已取消操作，未执行任何命令",
                    })
                _pending_safety_cmd = None
                continue

            # ===== 引擎配置管理 =====
            if msg_type == "get_config":
                cfg = get_config()
                await ws.send_json({"type": "engine_config", "config": cfg.to_dict()})
                continue

            if msg_type == "set_config":
                cfg = get_config()
                key = data.get("key", "")
                value = data.get("value")
                if key and value is not None:
                    cfg.set(key, value)
                    await ws.send_json({"type": "engine_config", "config": cfg.to_dict(), "saved": True})
                else:
                    await ws.send_json({"type": "error", "error": "缺少 key 或 value"})
                continue

            if msg_type == "reset_config":
                cfg = get_config()
                cfg._config = dict(cfg.DEFAULT_CONFIG)  # type: ignore
                cfg.save()
                await ws.send_json({"type": "engine_config", "config": cfg.to_dict(), "reset": True})
                continue

            if msg_type == "reset":
                conversation_history.clear()
                logger.info("对话历史已重置")
                await ws.send_json({"type": "chat_reset", "message": "对话已重置"})
                continue

            # ===== 流式 STT：分段音频 → 实时增量返回 =====
            if msg_type == "audio_stream":
                audio_b64 = data.get("audio", "")
                is_final = data.get("final", False)

                if is_final and not audio_b64:
                    # final=true 且无音频：结束标记
                    final_text = _stt_buffer.strip()
                    _stt_buffer = ""
                    if final_text:
                        await ws.send_json({"type": "stt_result", "text": final_text, "final": True})
                    continue

                if not audio_b64:
                    continue

                logger.info("STT 转写 (%d bytes, final=%s)", len(audio_b64), is_final)
                text = await stt_transcribe(audio_b64)
                if text:
                    text = stt_correct(text)
                    _stt_buffer += " " if _stt_buffer and not _stt_buffer.endswith(("。","！","？","，")) else ""
                    _stt_buffer += text
                    # 每次返回完整累积文本，前端直接替换输入框即可
                    await ws.send_json({"type": "stt_result", "text": _stt_buffer, "final": False})
                continue


            if msg_type == "chat":
                text = data.get("text", "").strip()
                if not text:
                    await ws.send_json({"type": "error", "error": "文本不能为空"})
                    continue

                logger.info(f"收到消息: {text[:60]}...")

                # ==== 审批检测（优先于意图分类，零延迟）====
                # 口语化确认词，去掉标点后精确匹配或短句前缀匹配
                APPROVAL_WORDS = {
                    "允许", "批准",                                    # 正式
                    "开始", "开始吧", "开工", "开干", "搞起", "走起",   # 启动
                    "可以", "可以了", "行", "行吧",                     # 同意
                    "好的", "好啊", "好吧", "好呀", "好嘞",             # 好字辈
                    "来吧", "上吧",                                    # 来/上
                    "做吧", "弄吧", "干吧",                             # 干活
                    "确定", "确认",                                     # 确认
                    "就这样", "就这么办",                               # 就这样
                    "ok", "OK", "okay", "go", "yes",                   # 英文
                }
                _clean = re.sub(r'[\s,，。！？、；：""''《》!?;:\'()\u3000]+', '', text)
                # 精确匹配，避免口语短词误触发
                _matched = _clean in APPROVAL_WORDS
                if _matched:
                    # 先检查是否有待安全确认的任务
                    pm = get_pending_manager()
                    task = pm.pop_next()
                    if task:
                        logger.info(f"Reasonix 任务已批准: {task.prompt[:50]}...")
                        # 安全过滤检查：对任务 prompt 做风险评估
                        risk = assess_risk(task.prompt)
                        if risk.level == "high":
                            logger.warning(f"安全拦截高风险任务: {risk.reasons}")
                            _pending_safety_cmd = task.prompt[:500]
                            await ws.send_json({
                                "type": "safety_confirm",
                                "command": task.prompt[:200],
                                "risk": risk.level,
                                "reasons": risk.reasons,
                                "message": format_confirm_message(risk),
                            })
                            continue

                        await ws.send_json({
                            "type": "route", "path": "reasonix", "reason": "Reasonix已批准，开始执行",
                        })
                        full_output = ""
                        async for line in reasonix_execute(task.prompt, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
                            await ws.send_json({"type": "token", "text": line})
                            full_output += line
                        await ws.send_json({"type": "done", "path": "reasonix", "reply": full_output.strip(), "model": "reasonix"})
                        await _maybe_tts(ws, full_output.strip(), "reasonix")
                        continue

                # ===== 意图分类 =====
                decision = classify(text)
                logger.info(f"路由决策: path={decision.path}, reason={decision.reason}")

                # 通知前端路由路径
                await ws.send_json({
                    "type": "route",
                    "path": decision.path,
                    "reason": decision.reason,
                })

                # ===== 定义 LLM 调用（含缓存逻辑） =====
                cache = get_cache()
                memory = get_memory()

                async def call_llm(prompt: str, route_path: str = "llm", auto_search: bool = False, prefer_cloud: bool = False):
                    # 0. 查缓存
                    cached = cache.check_and_get(prompt)
                    if cached:
                        logger.info(f"缓存命中: {prompt[:30]}...")
                        reply_text = cached["reply"]
                        # 防御：兼容旧缓存（reply 中可能残留 [ACTIONS] 标签）
                        clean_reply, cached_actions = parse_actions(reply_text)
                        if cached_actions:
                            reply_text = clean_reply
                            logger.info(f"缓存 [ACTIONS] 兜底解析: {json.dumps(cached_actions, ensure_ascii=False)[:200]}")
                        else:
                            # 新缓存：ACTIONS 存储在独立字段
                            actions_json = cached.get("actions_json")
                            if actions_json:
                                try:
                                    cached_actions = json.loads(actions_json)
                                    logger.info(f"缓存 [ACTIONS] 恢复: {actions_json[:200]}")
                                except json.JSONDecodeError:
                                    logger.warning(f"缓存 actions_json 解析失败: {actions_json[:100]}")
                        # 发送回复
                        await ws.send_json({"type": "token", "text": reply_text})
                        await ws.send_json({"type": "done", "path": "cache", "reply": reply_text, "model": "cache"})
                        asyncio.create_task(_maybe_tts(ws, reply_text, "cache"))
                        # 执行缓存中的音乐/设备操作
                        if cached_actions:
                            music_act = cached_actions.get("music")
                            device_acts = cached_actions.get("devices", [])
                            if music_act and isinstance(music_act, dict):
                                await _send_music_control(ws, music_act)
                                logger.info(f"[缓存] 执行音乐: {music_act}")
                            if device_acts:
                                for da in device_acts:
                                    virtual_home.execute(da.get("device", ""), da.get("action", "toggle"))
                                await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                        return

                    # 0.5 自动搜索（信息查询类）
                    mem_ctx = memory.get_context()

                    # 注入当前可用歌单列表（供 LLM 做歌单匹配）
                    try:
                        playlists_available = list_playlists()
                        if playlists_available:
                            names = "、".join(playlists_available.keys())
                            mem_ctx += (
                                f"\n\n【当前可用歌单 — 必须原样使用】{names}\n"
                                "规则：当用户请求播放音乐且未指定具体歌名时，你必须从上面列表中**原样复制**一个歌单名。\n"
                                "不确定选哪个时：学习/专注/焦虑/助眠→优先选含'轻音乐'的；日常/随便→优先选'收藏'。\n"
                                "**绝对不要**自己编造歌单名，不确定就留空 playlist 让后端自己选。"
                            )
                    except Exception:
                        pass  # 歌单目录可能尚未创建

                    if auto_search:
                        logger.info(f"自动搜索: {prompt[:40]}...")
                        await ws.send_json({
                            "type": "search_status",
                            "status": "searching",
                            "message": "正在联网搜索...",
                        })
                        search_ctx = await search_to_context(prompt)
                        if search_ctx:
                            mem_ctx += "\n" + search_ctx
                            await ws.send_json({
                                "type": "search_status",
                                "status": "done",
                                "message": "已获取搜索结果",
                                "result": search_ctx[:200] + ("..." if len(search_ctx) > 200 else ""),
                            })

                    # 1. 调大模型（记录实际使用的模型）
                    model_used: list[str] = []
                    actions_out: list[str] = []  # 侧通道：收集流式中的 [ACTIONS] 标签
                    full_reply = ""
                    try:
                        async for token in generate_stream(
                            prompt,
                            memory_context=mem_ctx,
                            conversation_history=conversation_history,
                            prefer_cloud=prefer_cloud,
                            model_used=model_used,
                            actions_out=actions_out,
                        ):
                            await ws.send_json({"type": "token", "text": token})
                            full_reply += token
                    except RuntimeError as e:
                        full_reply = strip_search_tags(full_reply)
                        logger.error(f"大模型调用失败: {e}")
                        await ws.send_json({"type": "error", "error": str(e)})
                        await ws.send_json({"type": "done", "path": route_path, "reply": full_reply, "model": model_used[0] if model_used else "unknown"})
                        await _maybe_tts(ws, full_reply, route_path)
                        return

                    # 处理 [ACTIONS] 标签：模型主动输出的设备/音乐操作
                    # 优先从侧通道读取（流式层已收集），兜底检查 full_reply
                    llm_actions = None
                    for action_text in actions_out:
                        _, actions = parse_actions(action_text)
                        if actions:
                            llm_actions = actions
                            logger.info(f"LLM 输出 [ACTIONS] (侧通道): {json.dumps(actions, ensure_ascii=False)[:200]}")
                            break
                    if not llm_actions and '[ACTIONS]' in full_reply:
                        full_reply, llm_actions = parse_actions(full_reply)
                        if llm_actions:
                            logger.info(f"LLM 输出 [ACTIONS] (兜底): {json.dumps(llm_actions, ensure_ascii=False)[:200]}")

                    full_reply = strip_search_tags(full_reply)
                    # 清洗 Markdown 格式字符（双保险：前端显示 + TTS 都不含格式符号）
                    full_reply = _clean_for_tts(full_reply)
                    actual_model = model_used[0] if model_used else "unknown"
                    logger.info(f"大模型回复完成, 模型: {actual_model}")
                    await ws.send_json({"type": "done", "path": route_path, "reply": full_reply.strip(), "model": actual_model})

                    asyncio.create_task(_maybe_tts(ws, full_reply.strip(), route_path))

                    # 执行 LLM 通过 [ACTIONS] 标签输出的操作
                    if llm_actions:
                        music_act = llm_actions.get("music")
                        device_acts = llm_actions.get("devices", [])
                        if music_act and isinstance(music_act, dict):
                            await _send_music_control(ws, music_act)
                            logger.info(f"[ACTIONS] 执行音乐: {music_act}")
                        if device_acts:
                            for da in device_acts:
                                virtual_home.execute(da.get("device", ""), da.get("action", "toggle"))
                            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})

                    # 4.5 兜底音乐检测：LLM 未输出 ACTIONS 但回复确认了播放
                    # 场景：小模型（qwen2.5:7b）有时忽略 ACTIONS 指令但仍会在文字中确认"轻音乐已就位"
                    if not llm_actions and route_path == "llm" and full_reply.strip():
                        music_reply_hints = [
                            "已经在放", "已就位", "已开始播放", "音乐已经", "已经为你",
                            "正在播放", "轻音乐已", "背景音乐已", "歌单已", "已为你播放",
                            "给你放", "为你放", "放点", "来点音乐", "帮你播放",
                        ]
                        reply_hints = any(hint in full_reply for hint in music_reply_hints)
                        user_music_kw = ["歌", "音乐", "音樂", "听歌", "聽歌", "放点", "放點",
                                        "来点", "來點", "播点", "播點", "放首", "来首", "來首",
                                        "放点", "放些", "播放", "想听", "想聽"]
                        user_wants = any(kw in prompt for kw in user_music_kw)
                        if reply_hints and user_wants:
                            logger.info("兜底音乐检测：LLM 回复暗示播放 + 用户请求音乐 → 触发歌单播放")
                            # 尝试从 LLM 回复中匹配可用歌单名
                            playlist_name = ""
                            try:
                                from playlist_service import list_playlists as _list_playlists
                                available = _list_playlists()
                                for pname in available:
                                    if pname in full_reply:
                                        playlist_name = pname
                                        logger.info(f"兜底歌单匹配: LLM 回复含 '{pname}'")
                                        break
                                if not playlist_name and available:
                                    # 无匹配 → 根据用户 prompt 关键词选
                                    prompt_lower = prompt.lower()
                                    for pname in available:
                                        if any(kw in prompt_lower for kw in ["学习", "专注", "轻音乐", "安静", "助眠"]):
                                            if "轻音乐" in pname:
                                                playlist_name = pname
                                                break
                                    if not playlist_name:
                                        playlist_name = list(available.keys())[0]
                                        logger.info(f"兜底歌单: 无匹配，默认选 '{playlist_name}'")
                            except Exception:
                                pass
                            await _send_music_control(ws, {"action": "play", "playlist": playlist_name})

                    # 5. 更新对话历史（非缓存路径）
                    reply_text = full_reply.strip()
                    if reply_text:
                        conversation_history.append({"role": "user", "content": prompt})
                        conversation_history.append({"role": "assistant", "content": reply_text})
                        # 限制最大对话轮数（最近 10 轮 = 20 条消息）
                        if len(conversation_history) > 20:
                            conversation_history[:2] = []  # 裁掉最早的一轮（user+assistant）

                    # 2. 用户原文隐含意图提取（仅从用户输入提取，不碰 LLM 回复）
                    #    LLM 若判定用户有明确操作意图 → 必须通过 [ACTIONS] 标签输出（上方已处理）
                    #    此处的 classify(prompt) 只处理用户原文中已有关键词但走了 llm 路由的情况
                    if route_path == "llm" and full_reply.strip():
                        implicit = classify(prompt)
                        if implicit.path == "xiaoai" and (implicit.device_actions or implicit.music_action):
                            # 校验音乐查询质量：拒绝明显不是歌曲名的 query
                            if implicit.music_action:
                                q = implicit.music_action.get("query", "")
                                if q and (len(q) > 30 or any(p in q for p in ["。", "！", "？", "，", "希望", "如果", "推荐", "告诉", "比如", "或", "之类"])):
                                    logger.info(f"用户原文音乐查询无效({q[:40]})，清空 query 走歌单回退")
                                    implicit.music_action["query"] = ""  # 不丢弃 action，清空 query 让后端走歌单/随机回退
                            if implicit.device_actions or implicit.music_action:
                                implicit_result = xiaoai_execute(
                                    virtual_home=virtual_home,
                                    device_actions=implicit.device_actions,
                                    text=prompt,
                                    matched_key=implicit.matched_key,
                                    music_action=implicit.music_action,
                                )
                                if implicit_result["handled"]:
                                    if implicit_result.get("music_action"):
                                        await _send_music_control(ws, implicit_result["music_action"])
                                    if implicit_result["results"]:
                                        await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                                    logger.info(f"用户原文隐含操作执行: devices={implicit.device_actions} music={implicit.music_action}")

                    # 3. 记忆提取（用户原文 + LLM 回复双向提取）
                    if route_path == "llm" and full_reply.strip():
                        # 从用户原文提取
                        new_memories = memory.extract_and_store(prompt)
                        # 也从 LLM 回复中提取（LLM 可能复述了用户信息）
                        new_memories += memory.extract_and_store(full_reply.strip())
                        if new_memories:
                            logger.info(f"新记忆: {new_memories}")
                            await ws.send_json({
                                "type": "memory_learned",
                                "memories": new_memories,
                                "message": "我记住了关于你的新信息",
                            })

                    # 4. 计数 + 可能缓存
                    if route_path == "llm" and full_reply.strip():
                        count, reached = cache.increment_and_check(prompt)
                        logger.info(f"LLM 计数: {prompt[:30]}... -> {count}/3")
                        if reached:
                            actions_json = json.dumps(llm_actions, ensure_ascii=False) if llm_actions else None
                            cache.store_reply(prompt, full_reply.strip(), actions_json=actions_json)
                            logger.info(f"缓存学习完成: {prompt[:30]}...")
                            await ws.send_json({
                                "type": "cache_learned",
                                "text": prompt[:50],
                                "message": "我记住了这个对话习惯，下次可以直接回答",
                            })

                # ===== 路径 A：设备控制 =====
                if decision.path == "xiaoai":
                    result = xiaoai_execute(
                        virtual_home=virtual_home,
                        device_actions=decision.device_actions,
                        text=text,
                        matched_key=decision.matched_key,
                        music_action=decision.music_action,
                    )

                    if result["handled"]:
                        await ws.send_json({"type": "token", "text": result["reply"]})
                        await ws.send_json({"type": "done", "path": "xiaoai", "reply": result["reply"], "model": "xiaoai"})
                        # 纯音乐动作不播乐观 TTS，由 _send_music_control 按实际结果播报
                        is_pure_music = result.get("music_action") and not result.get("results")
                        if not is_pure_music:
                            asyncio.create_task(_maybe_tts(ws, result["reply"], "xiaoai"))
                        if result.get("music_action"):
                            await _send_music_control(ws, result["music_action"])
                        if result["results"]:
                            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                        continue
                    else:
                        # 设备无法处理 → 直接调大模型
                        logger.info(f"设备无法处理，直接调用大模型: {text[:50]}")
                        await ws.send_json({"type": "route", "path": "llm", "reason": "设备无法处理，转交大模型"})
                        await call_llm(text, "llm")
                        continue

                # ===== 路径 noise：音乐串扰过滤，静默忽略 =====
                if decision.path == "noise":
                    logger.info(f"噪声过滤: 忽略疑似音乐串扰文本 '{text[:40]}'")
                    await ws.send_json({"type": "noise_filtered", "text": text[:40]})
                    continue

                # ===== 路径 B：信息查询（info_query → DeepSeek 云端 + 自动搜索） =====
                if decision.path == "info_query":
                    await ws.send_json({
                        "type": "route", "path": "llm", "reason": "信息查询",
                    })
                    await call_llm(text, "llm", auto_search=True)
                    continue

                # ===== 路径 C：混合意图（先设备后大模型） =====
                if decision.path == "mixed":
                    await ws.send_json({
                        "type": "route", "path": "mixed",
                        "reason": "混合意图，拆分子任务执行",
                    })
                    # 1. 先执行设备部分
                    for sub in decision.sub_tasks:
                        if sub["path"] == "xiaoai":
                            sub_result = xiaoai_execute(
                                virtual_home=virtual_home,
                                device_actions=sub.get("device_actions", []),
                                text=text,
                                matched_key="混合拆分",
                                music_action=sub.get("music_action"),
                            )
                            if sub_result["handled"]:
                                await ws.send_json({"type": "token", "text": sub_result["reply"]})
                                if sub_result.get("music_action"):
                                    await _send_music_control(ws, sub_result["music_action"])
                                if sub_result["results"]:
                                    await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})

                    # 2. 再调大模型处理剩余部分（走 DeepSeek）
                    await call_llm(text, "llm", auto_search=True)
                    continue

                # ===== 路径 D：Reasonix 执行（需审批） =====
                if decision.path == "reasonix":
                    if not is_reasonix_available():
                        logger.info("Reasonix CLI 不可用，fallback 到大模型")
                        await ws.send_json({"type": "route", "path": "llm", "reason": "Reasonix 未安装，转交大模型"})
                        await call_llm(text, "llm")
                        continue

                    # 创建待审批任务，等用户说"允许"
                    pm = get_pending_manager()
                    pm.add(text)
                    await ws.send_json({
                        "type": "pending_task",
                        "task": text[:80],
                        "message": "已记录编程任务，说「允许」开始执行",
                    })
                    await ws.send_json({
                        "type": "route", "path": "reasonix", "reason": "Reasonix待审批",
                    })
                    await ws.send_json({
                        "type": "done", "path": "reasonix",
                        "reply": "已记录编程任务，说「允许」让 Reasonix 开始工作",
                        "model": "reasonix",
                    })
                    continue

                # ===== 路径 C：大模型处理 =====
                if decision.path == "llm":
                    await call_llm(text, "llm")
                    continue

            # 未知消息类型
            await ws.send_json({"type": "error", "error": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("WebSocket 客户端已断开")
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}")
    finally:
        # 停止情境引擎
        await ce.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
