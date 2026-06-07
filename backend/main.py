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

# ── 加载 .env 环境变量（不依赖 python-dotenv） ──
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.isfile(_ENV_FILE):
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key, _val = _key.strip(), _val.strip()
            if _key and _key not in os.environ:
                os.environ[_key] = _val

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from services.intent_router import classify
from services.llm_service import generate_stream, check_model_available, check_deepseek_available, DEFAULT_MODEL
from services.xiaoai_service import execute as xiaoai_execute
from services.reasonix_executor import execute as reasonix_execute, is_reasonix_available, get_pending_manager
from services.cache_engine import get_cache
from services.memory_engine import get_memory
from services.stt_pipeline import transcribe as stt_transcribe, get_status as stt_status
from services.stt_corrector import correct as stt_correct, is_low_quality_stt, _is_hallucination
from services.tts_service import text_to_speech_base64, clean_for_tts
from services.context_engine import get_context_engine
from services.search_service import search_to_context
from services.safety_filter import assess_risk, format_confirm_message, requires_confirmation
from services.llm_service import strip_search_tags, parse_actions, _strip_actions_tags
from services.engine_config import get_config
from services.music_service import (
    clean_music_query, _user_requested_music, send_music_control,
)
from services.cet6_service import (
    select_random_paper, get_answers as cet6_get_answers,
    build_index as cet6_build_index, _load_index,
    send_cet6_paper, handle_cet6_action, get_cet6_session,
)
from services.cet6_online import (
    fetch_online_index, search_papers, download_paper, get_online_count,
)
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
    reply = clean_for_tts(reply)
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



# ═══════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════
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
    logger.info("扫描 CET-6 试卷索引...")
    cet6_build_index()
    logger.info("预加载 CET-6 在线索引（后台）...")
    asyncio.create_task(fetch_online_index())
    logger.info("三道路由分发已启用")
    yield


app = FastAPI(title="智能音箱_无实物 API", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health_check():
    model_ok = await check_model_available()
    deepseek_ok = await check_deepseek_available()
    stt = await stt_status()
    from services.intent_router import classify
    d = classify("测试路由")
    return JSONResponse({
        "status": "ok",
        "version": APP_VERSION,
        "model_available": model_ok,
        "model": DEFAULT_MODEL,
        "deepseek_available": deepseek_ok,
        "reasonix_available": is_reasonix_available(),
        "stt": stt,
        "router": "xiaoai | info_query | mixed | reasonix | llm",
        "last_route": d.path,
    })


# 音乐代理域名白名单（防止被用作开放代理/SSRF）
_ALLOWED_MUSIC_DOMAINS = {
    "music.126.net",
    "jamendo.com",
    "pixabay.com",
    "audio-ssl.itunes.apple.com",
    "localhost",
    "127.0.0.1",
}


@app.get("/api/proxy/music")
async def proxy_music(url: str = Query(..., description="要代理的音乐URL")):
    """代理第三方音乐资源，解决跨域播放问题（仅允许白名单域名）"""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    if hostname not in _ALLOWED_MUSIC_DOMAINS and not any(
        hostname.endswith("." + d) for d in _ALLOWED_MUSIC_DOMAINS
    ):
        logger.warning(f"音乐代理域名被拒: {hostname} (url={url[:80]})")
        return JSONResponse({"error": "域名不在白名单中"}, status_code=403)
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

    === 客户端 → 服务端（接收）===
    {"type": "chat", "text": "用户语音转写文本"}
    {"type": "ping"}                                  — 心跳
    {"type": "get_devices"}                           — 查询设备状态
    {"type": "get_time"}                              — 查询虚拟时间
    {"type": "set_time", "time": "2025-06-07 14:00"}  — 设置时间
    {"type": "set_time_speed", "speed": 60}           — 时间倍速
    {"type": "toggle_time_pause"}                     — 暂停/恢复时间
    {"type": "toggle_time_simulation"}                — 开关时间模拟
    {"type": "toggle_suppress_alerts"}                — 开关情境提醒
    {"type": "safety_reply", "reply": "允许", ...}    — 安全审批回复
    {"type": "list_cache"} / "delete_cache" / "list_memories" / "delete_memory" / "clear_memories"
    {"type": "get_config"} / "set_config"
    {"type": "service_status_request"}                — 查询服务可用性
    {"type": "reset_conversation"}                    — 重置对话历史
    {"type": "cancel"}                                — 唤醒词打断 LLM 生成
    {"type": "stt_stream"} / "stt_audio" / "stt_end"  — 流式/分段音频

    === 服务端 → 客户端（发送）===
    {"type": "route", "path": "xiaoai|llm|reasonix|info_query|mixed|cet6|noise", "reason": "..."}
    {"type": "token", "text": "..."}                  — LLM 流式 token
    {"type": "done", "path": "...", "reply": "...", "model": "..."}  — 回复完成
    {"type": "error", "error": "..."}                 — 错误
    {"type": "device_state", "devices": [...]}        — 10个虚拟设备状态
    {"type": "time_sync", "time": {...}}              — 虚拟时间同步
    {"type": "stt_status", "whisper_ready": bool, "xunfei_configured": bool}
    {"type": "music_control", "action": "play|pause|next|prev|stop", ...}  — 音乐播放控制
    {"type": "music_search_status", "status": "searching|not_found|copyright_blocked"}
    {"type": "tts_audio", "audio": "<base64>", "text": "..."}  — TTS 音频
    {"type": "tts_failed", "text": "...", "reason": "..."}     — TTS 降级通知
    {"type": "cet6_paper", "paper_id": "...", "pdf_url": "...", ...}  — 试卷下发
    {"type": "cet6_search_results", "results": [...]}  — 试卷搜索结果
    {"type": "cet6_answers", "pdf_url": "..."}  — 答案 PDF
    {"type": "chat_attachment", "label": "...", "url": "..."}  — 聊天气泡附件
    {"type": "chat_error", "message": "..."}  — 内联错误消息
    {"type": "search_status", "status": "searching|done", "message": "..."}  — 联网搜索状态
    {"type": "cache_learned", "text": "...", "message": "..."}  — 缓存学习通知
    {"type": "memory_learned", "memories": [...]}  — 记忆提取通知
    {"type": "service_status", "whisper_ready": bool, "netease_available": bool, ...}  — 服务状态
    {"type": "proactive_alert", "alert": {...}}  — 情境引擎主动提醒
    {"type": "safety_prompt", "cmd": "...", "risk": "high|medium|low", "prompt": "..."}  — 安全确认
    {"type": "cancelled"}  — LLM 生成已被打断
    {"type": "memory_list", "entries": [...]} / "cache_list", "entries": [...]
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
    # 发送 STT 引擎状态（离线/在线）
    _stt = await stt_status()
    await ws.send_json({
        "type": "stt_status",
        "whisper_ready": _stt["whisper_ready"],
        "xunfei_configured": _stt["xunfei_configured"],
        "primary_engine": _stt["primary_engine"],
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
    # 对话历史按路径隔离（避免 CET-6/音乐上下文污染日常对话）
    conversation_histories: dict[str, list[dict]] = {
        "llm": [],
        "cet6": [],
        "music": [],
    }

    def _get_history(route_path: str) -> list[dict]:
        """获取指定路径的对话历史（info_query/reasonix/mixed/xiaoai 共享 llm 历史）"""
        key = route_path if route_path in ("cet6", "music") else "llm"
        return conversation_histories[key]

    # 安全确认命令（模块级变量声明）
    global _pending_safety_cmd

    # 取消事件：唤醒词打断正在生成的 LLM 回复
    cancel_event = asyncio.Event()

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

            if msg_type == "service_status_request":
                stt_s = await stt_status()
                from services.tts_service import is_available as tts_available
                from services.netease_cloud_api import get_netease_api
                netease_ok = await get_netease_api().check_available()
                await ws.send_json({
                    "type": "service_status",
                    "whisper_ready": stt_s.get("whisper_ready", False),
                    "xunfei_configured": stt_s.get("xunfei_configured", False),
                    "netease_available": netease_ok,
                    "ollama_available": await check_model_available(),
                    "deepseek_available": await check_deepseek_available(),
                    "tts_available": await tts_available(),
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

            if msg_type == "cancel":
                # 唤醒词打断：取消正在进行的 LLM 流式生成
                logger.info("收到取消信号（唤醒词打断）")
                cancel_event.set()
                await ws.send_json({"type": "cancelled"})
                continue

            if msg_type == "reset":
                for h in conversation_histories.values():
                    h.clear()
                cancel_event.clear()  # 重置取消信号
                logger.info("对话历史已重置")
                await ws.send_json({"type": "chat_reset", "message": "对话已重置"})
                continue

            # ===== CET-6 在线搜索（前端直接触发） =====
            if msg_type == "cet6_online_search":
                query = data.get("query", "")
                year = data.get("year")
                month = data.get("month")
                logger.info(f"CET-6 在线搜索: query={query}, year={year}, month={month}")

                await fetch_online_index()
                results = search_papers(year=year, month=month, exclude_downloaded=False)
                await ws.send_json({
                    "type": "cet6_search_results",
                    "results": [
                        {
                            "paper_id": r["paper_id"],
                            "title": r["title"],
                            "year": r["year"],
                            "month": r["month"],
                            "set_num": r["set_num"],
                            "downloaded": r.get("downloaded", False),
                        }
                        for r in results
                    ],
                })
                continue

            # ===== CET-6 下载试卷（前端直接触发） =====
            if msg_type == "cet6_download_paper":
                paper_id = data.get("paper_id", "")
                if not paper_id:
                    await ws.send_json({"type": "error", "error": "缺少 paper_id"})
                    continue
                logger.info(f"CET-6 下载试卷: {paper_id}")
                await ws.send_json({"type": "route", "path": "cet6", "reason": "下载试卷"})
                downloaded = await download_paper(paper_id)
                if downloaded:
                    sess = get_cet6_session(id(ws))
                    sess.clear()
                    sess["paper_id"] = downloaded["id"]
                    await ws.send_json({
                        "type": "cet6_paper",
                        "paper_id": downloaded["id"],
                        "title": downloaded["title"],
                        "pdf_url": downloaded["pdf_url"],
                        "has_audio": downloaded.get("has_audio", False),
                        "audio_url": downloaded.get("audio_url", ""),
                        "has_answers": downloaded.get("has_answers", False),
                    })
                    # 同步发送附件消息到聊天气泡
                    await ws.send_json({
                        "type": "chat_attachment",
                        "label": f"📎 下载 {downloaded['title']} 真题",
                        "url": downloaded["pdf_url"],
                    })
                    if downloaded.get("has_audio") and downloaded.get("audio_url"):
                        await ws.send_json({
                            "type": "music_control", "action": "play",
                            "song_name": downloaded["title"] + " 听力音频",
                            "download_url": downloaded["audio_url"],
                            "source": "local",
                        })
                    await ws.send_json({
                        "type": "done", "path": "cet6",
                        "reply": f"已下载 {downloaded['title']}",
                        "model": "cet6",
                    })
                else:
                    await ws.send_json({
                        "type": "done", "path": "cet6",
                        "reply": "下载失败，请稍后重试",
                        "model": "cet6",
                    })
                continue

            # ===== CET-6 关闭面板：清除后端会话状态 =====
            if msg_type == "cet6_close":
                get_cet6_session(id(ws)).clear()
                logger.info("CET-6 会话已清除")
                continue

            # ===== 🆕 网易云按需获取播放 URL（前端切歌时实时请求） =====
            if msg_type == "get_netease_url":
                song_id = data.get("song_id", "")
                if song_id:
                    from services.netease_cloud_api import get_netease_api
                    api = get_netease_api()
                    if await api.check_available():
                        info = await api.get_play_url(song_id)
                        url = info.get("url", "")
                        await ws.send_json({
                            "type": "netease_song_url",
                            "song_id": song_id,
                            "url": url,
                        })
                    else:
                        await ws.send_json({
                            "type": "netease_song_url",
                            "song_id": song_id,
                            "url": "",
                        })
                continue

            # ===== B-3: 完整音频一次转写 =====
            if msg_type == "audio_stream":
                audio_b64 = data.get("audio", "")
                is_final = data.get("final", False)

                # B-3 主路径：完整音频（final=true, audio=完整WAV）
                if is_final and audio_b64:
                    logger.info("STT 完整转写 (%d bytes)", len(audio_b64))
                    text = await stt_transcribe(audio_b64)
                    if text:
                        text = stt_correct(text)
                    await ws.send_json({"type": "stt_result", "text": text or "", "final": True})
                    continue

                # 兼容旧增量模式：final=true 无音频 → 刷新缓冲区
                if is_final and not audio_b64:
                    final_text = _stt_buffer.strip()
                    _stt_buffer = ""
                    if final_text:
                        await ws.send_json({"type": "stt_result", "text": final_text, "final": True})
                    continue

                # 旧增量模式：非 final 音频片段
                if not audio_b64:
                    continue
                logger.info("STT 增量转写 (%d bytes)", len(audio_b64))
                text = await stt_transcribe(audio_b64)
                if text:
                    text = stt_correct(text)
                    _stt_buffer += " " if _stt_buffer and not _stt_buffer.endswith(("。","！","？","，")) else ""
                    _stt_buffer += text
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

                # ===== CET-6 会话前置拦截 =====
                _cet6_state = get_cet6_session(id(ws))


                # 答案请求
                if _cet6_state and _cet6_state.get("paper_id") and any(kw in text for kw in ["答案", "对答案", "核对答案", "检查答案", "参考答案", "看看答案"]):
                    logger.info(f"CET-6 答案请求: paper={_cet6_state['paper_id']}")
                    answers = cet6_get_answers(_cet6_state["paper_id"])
                    await ws.send_json({
                        "type": "cet6_answers",
                        "paper_id": _cet6_state["paper_id"],
                        "pdf_url": answers.get("pdf_url", ""),
                        "error": answers.get("error", ""),
                    })
                    await ws.send_json({
                        "type": "done", "path": "cet6",
                        "reply": "已展示答案，请核对" if "pdf_url" in answers else "抱歉，这份试卷暂无答案解析",
                        "model": "cet6",
                    })
                    continue

                # 在线搜索结果中选择下载
                if _cet6_state and _cet6_state.get("search_results") and any(kw in text for kw in ["下载", "就要", "就选", "要这个", "这套", "选这个", "第一套", "第二套", "第三套", "就要这个"]):
                    results = _cet6_state["search_results"]
                    # 尝试从文本中匹配套号
                    matched = None
                    set_map = {"第一套": "1", "第二套": "2", "第三套": "3", "1": "1", "2": "2", "3": "3"}
                    for kw, sn in set_map.items():
                        if kw in text:
                            matched = next((r for r in results if r.get("set_num") == sn), None)
                            if matched:
                                break
                    # 如果只有一个搜索结果，直接用
                    if not matched and len(results) == 1:
                        matched = results[0]
                    # 用 LLM 简短回复匹配信息，然后执行下载
                    if matched:
                        logger.info(f"CET-6 用户选择下载: {matched['paper_id']}")
                        await ws.send_json({"type": "route", "path": "cet6", "reason": "在线下载试卷"})
                        downloaded = await download_paper(matched["paper_id"])
                        if downloaded:
                            _cet6_state["paper_id"] = downloaded["id"]
                            _cet6_state.pop("search_results", None)
                            await ws.send_json({
                                "type": "cet6_paper",
                                "paper_id": downloaded["id"],
                                "title": downloaded["title"],
                                "pdf_url": downloaded["pdf_url"],
                                "has_audio": downloaded.get("has_audio", False),
                                "audio_url": downloaded.get("audio_url", ""),
                                "has_answers": downloaded.get("has_answers", False),
                            })
                            # 同步发送附件消息到聊天气泡
                            await ws.send_json({
                                "type": "chat_attachment",
                                "label": f"📎 下载 {downloaded['title']} 真题",
                                "url": downloaded["pdf_url"],
                            })
                            if downloaded.get("has_audio") and downloaded.get("audio_url"):
                                await ws.send_json({
                                    "type": "music_control", "action": "play",
                                    "song_name": downloaded["title"] + " 听力音频",
                                    "download_url": downloaded["audio_url"],
                                    "source": "local",
                                })
                            await ws.send_json({
                                "type": "done", "path": "cet6",
                                "reply": f"已下载 {downloaded['title']}，请开始练习吧",
                                "model": "cet6",
                            })
                        else:
                            await ws.send_json({
                                "type": "done", "path": "cet6",
                                "reply": "抱歉，下载失败了，请稍后重试",
                                "model": "cet6",
                            })
                    else:
                        # 模糊匹配，提示用户明确选择
                        titles = ", ".join(r.get("set_num", "?") for r in results)
                        await ws.send_json({
                            "type": "done", "path": "cet6",
                            "reply": f"请明确要哪一套？（可选: {titles}）",
                            "model": "cet6",
                        })
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

                async def call_llm(prompt: str, route_path: str = "llm", auto_search: bool = False, prefer_cloud: bool = False, extra_context: str = ""):
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
                        # 发送回复（防御性剥离 ACTIONS 标签）
                        reply_text = _strip_actions_tags(reply_text)
                        await ws.send_json({"type": "token", "text": reply_text})
                        await ws.send_json({"type": "done", "path": "cache", "reply": reply_text, "model": "cache"})
                        asyncio.create_task(_maybe_tts(ws, reply_text, "cache"))
                        # 执行缓存中的音乐/设备操作
                        if cached_actions:
                            music_act = cached_actions.get("music")
                            device_acts = cached_actions.get("devices", [])
                            if music_act and isinstance(music_act, dict):
                                if _user_requested_music(prompt):
                                    await send_music_control(ws, music_act, tts_callback=_maybe_tts)
                                    logger.info(f"[缓存] 执行音乐: {music_act}")
                                else:
                                    logger.warning(
                                        f"[缓存] 跳过幻觉音乐标签: {json.dumps(music_act, ensure_ascii=False)[:120]} "
                                        f"| 用户原文未包含音乐请求: {prompt[:80]}"
                                    )
                            if device_acts:
                                for da in device_acts:
                                    virtual_home.execute(da.get("device", ""), da.get("action", "toggle"))
                                await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                        return

                    # 0.5 自动搜索（信息查询类）
                    mem_ctx = memory.get_context()
                    if extra_context:
                        mem_ctx = extra_context + "\n" + mem_ctx if mem_ctx else extra_context

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
                    cancel_event.clear()  # 重置取消信号（新请求开始）
                    try:
                        async for token in generate_stream(
                            prompt,
                            memory_context=mem_ctx,
                            conversation_history=_get_history(route_path),
                            prefer_cloud=prefer_cloud,
                            model_used=model_used,
                            actions_out=actions_out,
                            cancel_event=cancel_event,
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

                    # 检查是否被取消（唤醒词打断）
                    if cancel_event.is_set():
                        logger.info("LLM 流被取消（唤醒词打断），丢弃部分回复")
                        await ws.send_json({"type": "cancelled"})
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
                    # 分离显示文本与 TTS 文本：
                    # 显示文本：只剥离 ACTIONS 标签，保留 emoji 和轻量格式（有人情味）
                    # TTS 文本：彻底清洗所有格式字符（纯文本朗读）
                    display_reply = _strip_actions_tags(full_reply)
                    tts_reply = clean_for_tts(display_reply)  # clean_for_tts 已内置 _strip_actions_tags
                    actual_model = model_used[0] if model_used else "unknown"
                    logger.info(f"大模型回复完成, 模型: {actual_model}")
                    await ws.send_json({"type": "done", "path": route_path, "reply": display_reply.strip(), "model": actual_model})

                    asyncio.create_task(_maybe_tts(ws, tts_reply.strip(), route_path))

                    # 执行 LLM 通过 [ACTIONS] 标签输出的操作
                    if llm_actions:
                        music_act = llm_actions.get("music")
                        device_acts = llm_actions.get("devices", [])
                        if music_act and isinstance(music_act, dict):
                            # 交叉验证：检查用户原文是否真的包含音乐请求
                            # 防止 LLM 将回复中的短语幻觉成歌曲名（如鼓励语中的"相信自己"）
                            if _user_requested_music(prompt):
                                await send_music_control(ws, music_act, tts_callback=_maybe_tts)
                                logger.info(f"[ACTIONS] 执行音乐: {music_act}")
                            else:
                                logger.warning(
                                    f"[ACTIONS] 跳过幻觉音乐标签: {json.dumps(music_act, ensure_ascii=False)[:120]} "
                                    f"| 用户原文未包含音乐请求: {prompt[:80]}"
                                )
                        if device_acts:
                            for da in device_acts:
                                virtual_home.execute(da.get("device", ""), da.get("action", "toggle"))
                            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})

                        # 执行 LLM 通过 [ACTIONS] 输出的 CET-6 操作（与音乐/设备控制一致的模式）
                        cet6_act = llm_actions.get("cet6")
                        if cet6_act and isinstance(cet6_act, dict):
                            await handle_cet6_action(ws, cet6_act, id(ws), tts_callback=_maybe_tts)


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
                            await send_music_control(ws, {"action": "play", "playlist": playlist_name}, tts_callback=_maybe_tts)

                    # 5. 更新对话历史（非缓存路径）
                    reply_text = full_reply.strip()
                    if reply_text:
                        history = _get_history(route_path)
                        history.append({"role": "user", "content": prompt})
                        history.append({"role": "assistant", "content": reply_text})
                        # 限制最大对话轮数（最近 5 轮 = 10 条消息，各路径独立）
                        if len(history) > 10:
                            history[:2] = []  # 裁掉最早的一轮（user+assistant）

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
                                        await send_music_control(ws, implicit_result["music_action"], tts_callback=_maybe_tts)
                                    if implicit_result["results"]:
                                        await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                                    logger.info(f"用户原文隐含操作执行: devices={implicit.device_actions} music={implicit.music_action}")

                    # 3. 记忆提取（用户原文 + LLM 回复双向提取）
                    if route_path == "llm" and full_reply.strip():
                        # 过滤低质量/幻觉 STT 文本，避免存入误识别记忆
                        if not is_low_quality_stt(prompt) and not _is_hallucination(prompt):
                            new_memories = memory.extract_and_store(prompt)
                        else:
                            logger.info(f"记忆跳过（低质量/幻觉文本）: {prompt[:40]}")
                            new_memories = []
                        # 也从 LLM 回复中提取（LLM 可能复述了用户信息）
                        if not _is_hallucination(full_reply.strip()):
                            new_memories += memory.extract_and_store(full_reply.strip())
                        if new_memories:
                            logger.info(f"新记忆（正则）: {new_memories}")
                            await ws.send_json({
                                "type": "memory_learned",
                                "memories": new_memories,
                                "message": "我记住了关于你的新信息",
                            })
                        # LLM 辅助记忆提取（异步，不阻塞主回复）
                        asyncio.create_task(memory.extract_with_llm(prompt, full_reply.strip()))

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
                            await send_music_control(ws, result["music_action"], tts_callback=_maybe_tts)
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
                                    await send_music_control(ws, sub_result["music_action"], tts_callback=_maybe_tts)
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
        get_cet6_session(id(ws)).clear()
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}")
        get_cet6_session(id(ws)).clear()
    finally:
        # 停止情境引擎
        await ce.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
