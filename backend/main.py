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

# ===== 非阻塞 TTS 辅助 =====
async def _maybe_tts(ws: WebSocket, reply: str, path: str):
    """非阻塞发送 TTS 音频，失败时通知前端降级 speechSynthesis"""
    if not reply or not reply.strip():
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


# ===== 音乐搜索缓存（供"换一首"使用） =====
_search_cache: dict[str, list[dict]] = {}  # query → 前3条外部搜索结果


def _clean_song_query(text: str) -> str:
    """清理搜索词：移除指令词、语气词，只保留歌名关键词"""
    import re as _re
    cleaned = _re.sub(r"^(播放|放|来一首|我想听|我要听|点一首|给我放|给我播|播)\s*", "", text)
    cleaned = _re.sub(r"[啊哦嗯呀吧啦呗么]+$", "", cleaned)
    return cleaned.strip()


# ===== 音乐控制辅助 =====
async def _send_music_control(ws, music_action: dict):
    """发送音乐控制消息，如果 play 时携带 query 则先搜索歌曲"""
    from services.music_service import MusicService
    from playlist_service import get_all_songs, search_local

    action = music_action.get("action", "play")
    query = music_action.get("query", "")

    if action == "play" and query:
        # 清理搜索关键词
        clean_query = _clean_song_query(query)
        logger.info(f"搜索歌曲: '{query}' (清理后: '{clean_query}')")

        # 1. 优先本地歌单搜索（含 difflib 模糊匹配）
        local_hit = search_local(clean_query) if clean_query else None
        if local_hit:
            song = {
                "song_id": f"local_{local_hit['filename']}",
                "song_name": local_hit["song_name"], "singers": local_hit["artist"],
                "album": "", "source": "local", "duration": "", "duration_s": 0,
                "cover_url": "", "download_url": local_hit["url"], "ext": "mp3",
                "file_size": "", "file_size_bytes": 0, "quality": "", "lyric": "",
            }
            await ws.send_json({
                "type": "music_control", "action": "play",
                "song_id": song["song_id"], "song_name": song["song_name"],
                "singers": song["singers"], "download_url": song["download_url"],
                "source": "local", "songs": [song],
            })
            logger.info(f"本地命中: {song['song_name']} - {song['singers']}")
            return

        # 2. 外部 API 搜索
        ms = MusicService()
        songs = await ms.search_songs(clean_query)
        if songs:
            first = songs[0]
            # 缓存前3条结果用于"换一首"
            _search_cache[clean_query] = songs[:3]
            await ws.send_json({
                "type": "music_control", "action": "play",
                "song_id": first.get("song_id", ""), "song_name": first.get("song_name", ""),
                "singers": first.get("singers", ""), "album": first.get("album", ""),
                "source": first.get("source", ""), "duration": first.get("duration", ""),
                "duration_s": first.get("duration_s", 0),
                "cover_url": first.get("cover_url", ""), "download_url": first.get("download_url", ""),
                "ext": first.get("ext", "mp3"), "songs": songs,
            })
            logger.info(f"音乐结果: {first.get('song_name', '')} by {first.get('singers', '')} ({len(songs)}条, 缓存前{len(songs[:3])}条)")
        else:
            logger.info(f"音乐搜索无结果: {query}，不触发播放")
            await ws.send_json({
                "type": "chat_error",
                "message": f"未找到歌曲「{query}」，请换个关键词试试",
            })
    elif action == "play" and not query:
        all_local = get_all_songs()
        if all_local:
            songs = []
            for s in all_local:
                songs.append({
                    "song_id": f"local_{s['filename']}", "song_name": s["song_name"],
                    "singers": s["artist"], "album": "", "source": "local",
                    "duration": "", "duration_s": 0, "cover_url": "",
                    "download_url": s["url"], "ext": "mp3",
                    "file_size": "", "file_size_bytes": 0, "quality": "", "lyric": "", "bitrate": "", "local": True,
                })
            first = songs[0]
            await ws.send_json({
                "type": "music_control", "action": "play",
                "song_name": first["song_name"], "singers": first["singers"],
                "download_url": first["download_url"], "songs": songs,
            })
            logger.info(f"播放全部本地歌单: {len(songs)} 首")
        else:
            logger.info("本地歌单为空，不触发播放")
            await ws.send_json({
                "type": "chat_error",
                "message": "本地歌单为空，请先添加音乐文件到 public/music/ 目录",
            })
    else:
        await ws.send_json({"type": "music_control", "action": action})


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
            return StreamingResponse(
                resp.aiter_bytes(),
                status_code=200,
                media_type=resp.headers.get("content-type", "audio/mpeg"),
                headers={"Content-Length": resp.headers.get("content-length", ""),
                         "Accept-Ranges": "bytes"},
            )
    except Exception as e:
        logger.warning(f"音乐代理失败: {e}")
        return JSONResponse({"error": "代理失败"}, status_code=502)


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

            # ===== 流式 STT：分段发到后端，实时返回转写结果 =====
            # ===== 语音转写：一次性接收完整音频，一次转写 =====
            if msg_type == "audio_stream":
                audio_b64 = data.get("audio", "")
                if not audio_b64:
                    continue
                logger.info("STT 转写 (%d bytes)", len(audio_b64))
                text = await stt_transcribe(audio_b64)
                if text:
                    await ws.send_json({"type": "stt_result", "text": text})
                else:
                    await ws.send_json({"type": "error", "error": "语音转写失败"})
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
                        await ws.send_json({"type": "token", "text": cached["reply"]})
                        await ws.send_json({"type": "done", "path": "cache", "reply": cached["reply"], "model": "cache"})
                        asyncio.create_task(_maybe_tts(ws, cached["reply"], "cache"))
                        return

                    # 0.5 自动搜索（信息查询类）
                    mem_ctx = memory.get_context()
                    if auto_search:
                        logger.info(f"自动搜索: {prompt[:40]}...")
                        await ws.send_json({
                            "type": "search_status",
                            "status": "searching",
                            "message": "🔍 正在联网搜索...",
                        })
                        search_ctx = await search_to_context(prompt)
                        if search_ctx:
                            mem_ctx += "\n" + search_ctx
                            await ws.send_json({
                                "type": "search_status",
                                "status": "done",
                                "message": "🔍 已获取搜索结果",
                                "result": search_ctx[:200] + ("..." if len(search_ctx) > 200 else ""),
                            })

                    # 1. 调大模型（记录实际使用的模型）
                    model_used: list[str] = []
                    full_reply = ""
                    try:
                        async for token in generate_stream(
                            prompt,
                            memory_context=mem_ctx,
                            conversation_history=conversation_history,
                            prefer_cloud=prefer_cloud,
                            model_used=model_used,
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

                    # 处理 [SEARCH] 标签：模型主动触发的搜索请求
                    if '[SEARCH]' in full_reply or '[/SEARCH]' in full_reply:
                        import re as _re
                        queries = _re.findall(r'\[SEARCH\](.*?)\[/SEARCH\]', full_reply, _re.DOTALL)
                        for q in queries:
                            q = q.strip()
                            if q:
                                logger.info(f"模型触发搜索: {q}")
                                await ws.send_json({
                                    "type": "search_status",
                                    "status": "searching",
                                    "message": f"🔍 正在搜索: {q[:30]}...",
                                })
                                result = await search_to_context(q)
                                if result:
                                    full_reply = full_reply.replace(f'[SEARCH]{q}[/SEARCH]', '', 1)
                                    full_reply += f"\n\n📎 搜索结果 ({q}):\n{result[:500]}"
                                else:
                                    full_reply = full_reply.replace(f'[SEARCH]{q}[/SEARCH]', '（搜索无结果）', 1)

                    # 处理 [ACTIONS] 标签：模型主动输出的设备/音乐操作
                    llm_actions = None
                    if '[ACTIONS]' in full_reply:
                        full_reply, llm_actions = parse_actions(full_reply)
                        if llm_actions:
                            logger.info(f"LLM 输出 [ACTIONS]: {json.dumps(llm_actions, ensure_ascii=False)[:200]}")

                    full_reply = strip_search_tags(full_reply)
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
                                    logger.info(f"用户原文音乐查询无效({q[:40]})，丢弃")
                                    implicit.music_action = None
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
                            cache.store_reply(prompt, full_reply.strip())
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
                        "reply": "📋 已记录编程任务，说「允许」让Reasonix开始工作",
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
