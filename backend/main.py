"""
智能音箱_无实物 —— 后端服务入口 (v0.3.0)
FastAPI + WebSocket 实现 AI 语音交互中枢（三道路由分发）

v0.3.0 变更:
  - STT: SenseVoice-Small 替换 faster-whisper
  - TTS: Kokoro TTS 离线主力 + Edge TTS 云端备选
  - 架构: WebSocket handler 拆分至 handlers/ 包
  - 安全: 硬编码 Key 迁移至环境变量、SSRF 修复、端口统一
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
import time
from contextlib import asynccontextmanager

# ── 统一配置 ──
from config import BACKEND_PORT as PORT, BACKEND_HOST, ALLOWED_MUSIC_DOMAINS

# ── 加载 .env 环境变量（不依赖 python-dotenv）──
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

# ── Handler 模块 ──
from handlers.stt_handler import handle_audio_stream
from handlers.cet6_handler import (
    handle_cet6_online_search, handle_cet6_download_paper,
    handle_cet6_close, handle_cet6_answers_in_chat, handle_cet6_download_in_chat,
)
import handlers.chat_handler as _chat_handler
from handlers.control_handler import (
    handle_service_status_request, handle_device_request, handle_time_request,
    handle_set_time, handle_set_time_speed, handle_toggle_time_pause,
    handle_toggle_time_simulation, handle_toggle_suppress_alerts,
    handle_list_cache, handle_delete_cache, handle_list_memories,
    handle_delete_memory, handle_clear_memories, handle_get_config,
    handle_set_config, handle_reset_config,
)

# ── 日志 ──
from config import LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT
os.makedirs(LOG_DIR, exist_ok=True)
from logging.handlers import RotatingFileHandler

# ★ 先调用 basicConfig（添加 StreamHandler → 控制台输出）
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ★ 再追加 RotatingFileHandler（写入文件）
_file_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"),
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_file_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)


# ===== ⚠️ 关键诊断：验证 sherpa_onnx 是否可用 =====
_STT_AVAILABLE = False
try:
    import sherpa_onnx
    _STT_AVAILABLE = True
    logger.info("✅ sherpa-onnx %s 可用 — STT 将正常工作", getattr(sherpa_onnx, '__version__', 'unknown'))
except ImportError:
    logger.critical("❌❌❌ sherpa-onnx 未安装！STT 将完全无法工作！")
    logger.critical("❌❌❌ 请使用 venv Python 启动：.venv\\Scripts\\python -m uvicorn main:app")
    print("\n" + "=" * 60)
    print("  ⚠️  严重警告：sherpa-onnx 未安装！")
    print("  STT 语音转写将无法工作，用户语音输入不会有任何回复。")
    print("  请使用: .venv\\Scripts\\python -m uvicorn main:app --host 0.0.0.0 --port 8000")
    print("=" * 60 + "\n")


# ===== 强制清理旧进程 =====
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
_stt_buffer: str = ""

# ===== TTS 回声过滤：记录近期播报文本，防止扬声器→麦克风自触发唤醒 =====
_recent_tts_texts: list[tuple[float, str]] = []  # (timestamp, normalized_text)
_TTS_ECHO_TTL = 8.0  # 8 秒后过期
_last_tts_time: float = 0  # ★ B2: 最近一次 TTS 播报时间戳，用于冷却期判断
_TTS_WAKE_COOLDOWN = 3.0  # ★ B2: TTS 播报后 3 秒内拒绝唤醒验证

def _record_tts_text(text: str):
    """记录 TTS 播报文本，供唤醒验证时过滤回声"""
    global _last_tts_time
    if not text or len(text) < 3:
        return
    _last_tts_time = time.monotonic()  # ★ B2: 更新时间戳
    _recent_tts_texts.append((time.monotonic(), text))
    # 清理过期（>8s）
    now = time.monotonic()
    _recent_tts_texts[:] = [(t, txt) for t, txt in _recent_tts_texts if now - t < _TTS_ECHO_TTL]

def _is_tts_echo(stt_text: str) -> bool:
    """检查 STT 结果是否为近期 TTS 播报的回声"""
    if not stt_text or len(stt_text) < 2:
        return False
    stt_norm = re.sub(r'[\s,，。！？、；：""''《》!?;:\'()　a-zA-Z]+', '', stt_text)
    if len(stt_norm) < 2:
        return False
    now = time.monotonic()
    for t, tts_text in _recent_tts_texts:
        if now - t > _TTS_ECHO_TTL:
            continue
        tts_norm = re.sub(r'[\s,，。！？、；：""''《》!?;:\'()　a-zA-Z]+', '', tts_text)
        if len(tts_norm) < 3:
            continue
        # 子串匹配（任一方向）
        if stt_norm in tts_norm or tts_norm in stt_norm:
            return True
        # 字符重叠 > 60%
        common = sum(1 for c in stt_norm if c in tts_norm)
        if common / max(len(stt_norm), 1) > 0.6:
            return True
    return False

# ===== 非阻塞 TTS 辅助 =====
async def _maybe_tts(ws: WebSocket, reply: str, path: str, seq: int = 0):
    """非阻塞发送 TTS 音频，失败时通知前端降级 speechSynthesis"""
    if not reply or not reply.strip():
        return
    reply = clean_for_tts(reply)
    if not reply:
        return
    _record_tts_text(reply)  # ★ 记录 TTS 文本用于回声过滤
    try:
        audio_b64 = await text_to_speech_base64(reply)
        if audio_b64:
            await ws.send_json({
                "type": "tts_audio",
                "audio": audio_b64,
                "text": reply[:120],
                "path": path,
                "seq": seq,
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


# ===== CET-6 路由直接执行 =====
_CET6_YEAR_RE = re.compile(r'(20\d{2})\s*年')
_CET6_MONTH_RE = re.compile(r'(\d{1,2})\s*月')
_CET6_SET_RE = re.compile(r'第\s*([一二三123])\s*套')


def _parse_cet6_intent(text: str) -> dict:
    """从用户文本中解析 CET-6 操作意图，返回 action dict（供 handle_cet6_action 使用）"""
    year_match = _CET6_YEAR_RE.search(text)
    month_match = _CET6_MONTH_RE.search(text)
    set_match = _CET6_SET_RE.search(text)

    year = int(year_match.group(1)) if year_match else None
    month = int(month_match.group(1)) if month_match else None
    set_cn = set_match.group(1) if set_match else None
    set_map = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
    set_num = set_map.get(set_cn) if set_cn else None

    # 确定 action 类型
    t = text.lower()

    # 做题 / 做真题 / 做卷子 → random_paper（有年份则精确匹配）
    if any(kw in t for kw in ["做真题", "做题", "做卷子", "来一套", "刷题",
                                "来一份", "给我真题", "给我试卷", "给套",
                                "我要做", "我想做", "想做", "要做"]):
        if year and month and set_num:
            return {"action": "paper", "year": year, "month": month, "set": set_num}
        elif year and month:
            return {"action": "random_paper", "year": year, "month": month}
        elif year:
            return {"action": "random_paper", "year": year}
        else:
            return {"action": "random_paper"}

    # 精确指定套题 → paper
    if year and month and set_num:
        return {"action": "paper", "year": year, "month": month, "set": set_num}

    # 浏览题库
    if any(kw in t for kw in ["浏览", "有哪些", "有什么", "题库", "看看", "都有"]):
        return {"action": "browse"}

    # 对答案
    if any(kw in t for kw in ["对答案", "看答案", "核对答案", "答案"]):
        return {"action": "answers"}

    # 播放听力
    if any(kw in t for kw in ["播放听力", "放听力", "听听力", "听力"]):
        if not any(kw in t for kw in ["怎么练", "如何提高", "技巧", "方法"]):
            return {"action": "listening"}

    # 搜索真题
    if any(kw in t for kw in ["搜索", "联网找", "找真题", "有没有"]):
        action = {"action": "search"}
        if year:
            action["year"] = year
        return action

    # 默认：用户提到了六级相关 → 随机来一套
    return {"action": "random_paper"}


async def _handle_cet6_route(ws: WebSocket, text: str):
    """CET-6 路由直接执行：解析用户意图 → 直接操作试卷/听力/答案"""
    try:
        action = _parse_cet6_intent(text)
        session_id = id(ws)
        logger.info(f"CET-6 路由直接执行: action={action.get('action')}, year={action.get('year')}, month={action.get('month')}")
        await handle_cet6_action(ws, action, session_id, tts_callback=_maybe_tts)
    except Exception as e:
        logger.error(f"CET-6 路由执行失败: {e}")
        await ws.send_json({
            "type": "done", "path": "cet6",
            "reply": "抱歉，试卷系统出了点问题，请再说一次试试",
            "model": "cet6",
        })


# ═══════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════
virtual_home = VirtualHome()
_pending_safety_cmd: str | None = None

APP_VERSION = "0.3.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = f"""
========================================
  智能音箱后端 v{APP_VERSION}
  三道路由: 设备 | 大模型 | Reasonix
  STT: SenseVoice-Small | TTS: Kokoro+Edge
========================================
"""
    print(banner)
    logger.info(f"后端 v{APP_VERSION} 启动中")
    logger.info("正在检查 Ollama 模型...")
    if await check_model_available():
        logger.info(f"模型 {DEFAULT_MODEL} 已就绪")
        # ★ 预热：发送微型请求让 Ollama 提前加载模型到 GPU/内存
        try:
            import ollama
            logger.info(f"预热本地模型 {DEFAULT_MODEL}（防止首 token 超时）...")
            await asyncio.to_thread(
                ollama.chat,
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": "hi"}],
                options={"num_predict": 1, "temperature": 0},
            )
            logger.info(f"模型 {DEFAULT_MODEL} 预热完成 ✓")
        except Exception as e:
            logger.warning(f"模型预热失败（不影响使用）: {e}")
    else:
        logger.warning(f"模型 {DEFAULT_MODEL} 未找到")
    logger.info(f"Reasonix CLI 可用: {is_reasonix_available()}")
    deepseek_ok = await check_deepseek_available()
    if deepseek_ok:
        logger.info("DeepSeek 云端 API 可用 ✓（搜索/查询将路由到云端）")
    else:
        logger.warning("DeepSeek 云端 API 不可用（请检查 .env 中的 DEEPSEEK_API_KEY）")
    logger.info("预加载 SenseVoice STT 模型（后台下载）...")
    from services.sensevoice_stt import preload_model
    await preload_model()
    logger.info("预加载 Kokoro TTS 模型（后台下载）...")
    from services.qwen3_tts import preload_model as preload_tts
    await preload_tts()
    logger.info("扫描 CET-6 试卷索引...")
    cet6_build_index()
    logger.info("预加载 CET-6 在线索引（后台）...")
    asyncio.create_task(fetch_online_index())
    logger.info("三道路由分发已启用")
    yield


app = FastAPI(title="智能音箱_无实物 API", version=APP_VERSION, lifespan=lifespan)


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


# ── 音乐代理（SSRF 安全：仅白名单域名）──
@app.get("/api/proxy/music")
async def proxy_music(url: str = Query(..., description="要代理的音乐URL")):
    """代理第三方音乐资源，解决跨域播放问题（仅允许白名单域名）"""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    # 安全检查：精确匹配 + 子域名匹配
    allowed = False
    if hostname in ALLOWED_MUSIC_DOMAINS:
        allowed = True
    else:
        for d in ALLOWED_MUSIC_DOMAINS:
            if hostname.endswith("." + d):
                allowed = True
                break
    if not allowed:
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
    from playlist_service import list_playlists
    return {"playlists": list_playlists()}


@app.get("/api/playlists/{name}")
async def api_get_playlist(name: str):
    from playlist_service import get_playlist
    songs = get_playlist(name)
    return {"name": name, "songs": songs or []}


@app.post("/api/playlists/{name}/refresh")
async def api_refresh_playlist(name: str):
    from playlist_service import refresh_playlists
    refresh_playlists()
    return {"ok": True}


@app.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket 端点 —— 三道路由分发中枢 (v0.3.0)

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
    """
    await ws.accept()
    global _stt_buffer
    logger.info("WebSocket 客户端已连接")

    # 初始状态推送
    await ws.send_json({
        "type": "device_state",
        "devices": virtual_home.get_all_states(),
    })
    vt = get_virtual_time()
    await ws.send_json({"type": "time_sync", "time": vt.to_dict()})

    _stt = await stt_status()
    await ws.send_json({
        "type": "stt_status",
        "whisper_ready": _stt["local_ready"],
        "local_ready": _stt["local_ready"],
        "local_engine": _stt.get("local_engine", "none"),
        "xunfei_configured": _stt["xunfei_configured"],
        "primary_engine": _stt["primary_engine"],
    })

    # 情境引擎
    async def _on_alert(alert: dict):
        try:
            await ws.send_json({"type": "proactive_alert", "alert": alert})
            logger.info(f"主动提醒推送: {alert.get('reason', '')}")
        except Exception:
            pass

    ce = get_context_engine(
        get_virtual_time=get_virtual_time,
        get_memory_engine=lambda: get_memory(),
        get_virtual_home=lambda: virtual_home,
    )
    ce.set_alert_callback(_on_alert)
    await ce.start(interval_seconds=30)

    # 推送提醒引擎初始状态
    try:
        alert_status = ce.get_status()
        await ws.send_json({"type": "alert_status", **alert_status})
    except Exception:
        pass

    # 对话历史（按路径隔离）
    conversation_histories: dict[str, list[dict]] = {
        "llm": [], "cet6": [], "music": [],
    }

    def _get_history(route_path: str) -> list[dict]:
        key = route_path if route_path in ("cet6", "music") else "llm"
        return conversation_histories[key]

    global _pending_safety_cmd
    cancel_event = asyncio.Event()
    _last_cancel_at: float = 0.0  # cancel 后的静默期起点
    _cancel_cooldown_s = 4.0       # cancel 后 N 秒内拒绝 chat 消息（防止噪音 STT 触发 LLM）
    _wake_just_verified = False    # ★ 刚通过唤醒验证 → skip 下一次 cancel 的静默期
    from playlist_service import list_playlists

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "消息格式错误"})
                continue

            msg_type = data.get("type", "")

            # ═══════════════════════════════════
            # 简单消息类型 → dispatch 到 handler
            # ═══════════════════════════════════

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if msg_type == "get_devices":
                await handle_device_request(ws, virtual_home)
                continue

            if msg_type == "service_status_request":
                await handle_service_status_request(ws)
                continue

            if msg_type == "list_cache":
                await handle_list_cache(ws)
                continue

            if msg_type == "delete_cache":
                await handle_delete_cache(ws, data)
                continue

            if msg_type == "list_memories":
                await handle_list_memories(ws)
                continue

            if msg_type == "delete_memory":
                await handle_delete_memory(ws, data)
                continue

            if msg_type == "clear_memories":
                await handle_clear_memories(ws)
                continue

            # 时间控制
            if msg_type == "get_time":
                await handle_time_request(ws)
                continue

            if msg_type == "set_time":
                await handle_set_time(ws, data)
                continue

            if msg_type == "set_time_speed":
                await handle_set_time_speed(ws, data)
                continue

            if msg_type == "toggle_time_pause":
                await handle_toggle_time_pause(ws)
                continue

            if msg_type == "toggle_time_simulation":
                await handle_toggle_time_simulation(ws, data)
                continue

            if msg_type == "toggle_suppress_alerts":
                await handle_toggle_suppress_alerts(ws, data, ce)
                continue

            # 安全确认回复
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

            # 引擎配置
            if msg_type == "get_config":
                await handle_get_config(ws)
                continue

            if msg_type == "set_config":
                await handle_set_config(ws, data)
                continue

            if msg_type == "reset_config":
                await handle_reset_config(ws)
                continue

            if msg_type == "set_engine_mode":
                mode = data.get("mode", "auto")
                if mode in ("auto", "local", "cloud"):
                    get_config().set("user_mode", mode)
                    logger.info(f"引擎模式切换: {mode}")
                    await ws.send_json({"type": "engine_mode_changed", "mode": mode})
                continue

            if msg_type == "get_engine_mode":
                mode = get_config().get("user_mode", "auto")
                await ws.send_json({"type": "engine_mode", "mode": mode})
                continue

            if msg_type == "verify_wake":
                # ★ 唤醒词验证：Mellon 已做声纹匹配，此处只需确认有真实人声（非环境噪声）
                #   录制的是唤醒词触发后 2.5 秒音频 → 不检查"小智"（已过时序窗口）
                #   只检查是否包含有效中文语音 → 有 = 真人 → 通过；空/噪声 = 假触发 → 拒绝
                audio_b64 = data.get("audio", "")
                if audio_b64 and len(audio_b64) > 100:
                    from services.stt_pipeline import transcribe as stt_transcribe
                    verify_text = await stt_transcribe(audio_b64)
                    logger.info(f"唤醒词验证 STT: {verify_text[:40] if verify_text else '(空)'}")
                    # ★ 新策略：检测有效中文语音（≥2 个中文字符），而非匹配特定词
                    if verify_text and verify_text.strip():
                        # ★ B2: TTS 播报后冷却期内拒绝所有唤醒验证
                        #   （前端 B1 失聪期是第一道防线，此处是后端兜底）
                        _since_tts = time.monotonic() - _last_tts_time
                        if _since_tts < _TTS_WAKE_COOLDOWN:
                            logger.info(
                                f"🔇 唤醒词验证拒绝 (TTS冷却期 {_since_tts:.1f}s < {_TTS_WAKE_COOLDOWN}s): "
                                f"{verify_text[:40]}"
                            )
                            await ws.send_json({
                                "type": "wake_rejected",
                                "text": verify_text,
                                "reason": "tts_cooldown",
                            })
                            continue
                        # ★ 原有 TTS 回声过滤：文本匹配兜底（冷却期外的二次确认）
                        if _is_tts_echo(verify_text):
                            logger.info(f"🔇 唤醒词验证拒绝 (TTS回声): {verify_text[:40]}")
                            await ws.send_json({"type": "wake_rejected", "text": verify_text, "reason": "tts_echo"})
                            continue
                        chinese_chars = sum(1 for c in verify_text if '一' <= c <= '鿿')
                        # 至少 2 个中文字符 → 确认真实人声（过滤单字碎片/纯英文/纯噪声）
                        if chinese_chars >= 2:
                            # ★ D: 唤醒词相关字符检测 — 短文本（<6 中文字符）需包含唤醒词相关字符
                            #   过滤音乐歌词/环境噪声等非用户语音的碎片中文（如"嫂子嫂子"、"小鸡小鸡"）
                            #   长文本（≥6 中文字符）通常是完整语句，无需此约束
                            _WAKE_RELATED_CHARS = set('小智')
                            if chinese_chars < 6:
                                _text_chars = set(c for c in verify_text if '一' <= c <= '鿿')
                                _overlap = _text_chars & _WAKE_RELATED_CHARS
                                if not _overlap:
                                    logger.info(
                                        f"❌ 唤醒词验证拒绝 (短文本无唤醒词相关字符 chinese={chinese_chars}): "
                                        f"{verify_text[:40]}"
                                    )
                                    await ws.send_json({
                                        "type": "wake_rejected",
                                        "text": verify_text,
                                        "reason": "no_wake_related_chars",
                                    })
                                    continue
                            logger.info(f"✅ 唤醒词验证通过 (中文字符={chinese_chars}): {verify_text[:40]}")
                            _wake_just_verified = True  # ★ 标记：跳过后续 cancel 的静默期
                            await ws.send_json({"type": "wake_verified", "text": verify_text})
                        else:
                            logger.info(f"❌ 唤醒词验证拒绝 (中文字符不足={chinese_chars}): {verify_text[:40]}")
                            await ws.send_json({"type": "wake_rejected", "text": verify_text or ""})
                    else:
                        logger.info("唤醒词验证: STT 返回空/静音，拒绝")
                        await ws.send_json({"type": "wake_rejected", "text": "", "reason": "silence"})
                else:
                    logger.info("唤醒词验证: 音频数据为空或过短，拒绝")
                    await ws.send_json({"type": "wake_rejected", "text": "", "reason": "no_audio"})
                continue

            if msg_type == "cancel":
                # 唤醒词防抖：生成开始后 5 秒内忽略取消信号，防止假阳性打断
                _elapsed = time.monotonic() - _chat_handler._last_gen_started_at
                if _elapsed < 5.0:
                    logger.info(
                        "唤醒词打断被防抖忽略 (生成已开始 %.1fs，< 5s)",
                        _elapsed,
                    )
                    continue
                logger.info("收到取消信号（唤醒词打断）")
                cancel_event.set()
                # ★ PCM 缓冲方案下，cancel 仅由已验证的唤醒词触发，
                # 后续录音始终是用户意图，无需静默期
                _wake_just_verified = False
                await ws.send_json({"type": "cancelled"})
                continue

            if msg_type == "reset":
                for h in conversation_histories.values():
                    h.clear()
                cancel_event.clear()
                logger.info("对话历史已重置")
                await ws.send_json({"type": "chat_reset", "message": "对话已重置"})
                continue

            # CET-6 在线搜索
            if msg_type == "cet6_online_search":
                await handle_cet6_online_search(ws, data)
                continue

            # CET-6 下载
            if msg_type == "cet6_download_paper":
                await handle_cet6_download_paper(ws, data)
                continue

            # CET-6 关闭
            if msg_type == "cet6_close":
                await handle_cet6_close(ws)
                continue

            # 网易云播放 URL
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

            # 音频流
            if msg_type == "audio_stream":
                _stt_buffer = await handle_audio_stream(ws, data, _stt_buffer)
                continue

            # ═══════════════════════════════════
            # 聊天消息 —— 核心路由分发
            # ═══════════════════════════════════
            if msg_type == "chat":
                text = data.get("text", "").strip()
                if not text:
                    await ws.send_json({"type": "error", "error": "文本不能为空"})
                    continue

                # Cancel 后静默期：防止虚假唤醒触发的噪音录音被当作 chat 处理
                _since_cancel = time.monotonic() - _last_cancel_at
                if _since_cancel < _cancel_cooldown_s:
                    logger.info(
                        "Chat 消息被静默期拦截 (cancel 后 %.1fs，< %.0fs): %s",
                        _since_cancel, _cancel_cooldown_s, text[:40],
                    )
                    continue

                logger.info(f"收到消息: {text[:60]}...")

                # ── 审批检测 ──
                APPROVAL_WORDS = {
                    "允许", "批准",
                    "开始", "开始吧", "开工", "开干", "搞起", "走起",
                    "可以", "可以了", "行", "行吧",
                    "好的", "好啊", "好吧", "好呀", "好嘞",
                    "来吧", "上吧",
                    "做吧", "弄吧", "干吧",
                    "确定", "确认",
                    "就这样", "就这么办",
                    "ok", "OK", "okay", "go", "yes",
                }
                _clean = re.sub(r'[\s,，。！？、；：""''《》!?;:\'()　]+', '', text)
                # ★ 前缀匹配：允许"允许执行""开始吧"等变体通过
                _matched = any(_clean.startswith(w) for w in APPROVAL_WORDS)
                if _matched:
                    pm = get_pending_manager()
                    task = pm.pop_next()
                    if task:
                        logger.info(f"Reasonix 任务已批准: {task.prompt[:50]}...")
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
                            "type": "route", "path": "reasonix", "reason": "Reasonix已批准",
                        })
                        full_output = ""
                        async for line in reasonix_execute(
                            task.prompt,
                            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ):
                            await ws.send_json({"type": "token", "text": line})
                            full_output += line
                        await ws.send_json({
                            "type": "done", "path": "reasonix",
                            "reply": full_output.strip(), "model": "reasonix",
                        })
                        await _maybe_tts(ws, full_output.strip(), "reasonix")
                        continue

                # ── CET-6 会话拦截 ──
                if await handle_cet6_answers_in_chat(ws, text):
                    continue
                if await handle_cet6_download_in_chat(ws, text):
                    continue

                # ── 意图分类 ──
                decision = classify(text)
                logger.info(f"路由决策: path={decision.path}, reason={decision.reason}")

                await ws.send_json({
                    "type": "route",
                    "path": decision.path,
                    "reason": decision.reason,
                })

                memory = get_memory()

                # ── 路径 xiaoai：设备控制 ──
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
                        await ws.send_json({
                            "type": "done", "path": "xiaoai",
                            "reply": result["reply"], "model": "xiaoai",
                        })
                        is_pure_music = result.get("music_action") and not result.get("results")
                        if not is_pure_music:
                            asyncio.create_task(_maybe_tts(ws, result["reply"], "xiaoai"))
                        if result.get("music_action"):
                            await send_music_control(ws, result["music_action"], tts_callback=_maybe_tts)
                        if result["results"]:
                            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                        continue
                    else:
                        logger.info(f"设备无法处理，直接调用大模型: {text[:50]}")
                        await ws.send_json({"type": "route", "path": "llm", "reason": "设备无法处理，转交大模型"})
                        await _chat_handler.call_llm(
                            ws, text, "llm",
                            memory=memory,
                            virtual_home=virtual_home,
                            conversation_histories=conversation_histories,
                            cancel_event=cancel_event,
                            maybe_tts_callback=_maybe_tts,
                            playlist_list_func=list_playlists,
                        )
                        continue

                # ── 路径 noise：静默忽略 ──
                if decision.path == "noise":
                    logger.info(f"噪声过滤: {text[:40]}")
                    await ws.send_json({"type": "noise_filtered", "text": text[:40]})
                    continue

                # ── 路径 info_query：信息查询 ──
                if decision.path == "info_query":
                    await ws.send_json({"type": "route", "path": "llm", "reason": "信息查询"})
                    await _chat_handler.call_llm(
                        ws, text, "llm", auto_search=True,
                        memory=memory,
                        virtual_home=virtual_home,
                        conversation_histories=conversation_histories,
                        cancel_event=cancel_event,
                        maybe_tts_callback=_maybe_tts,
                        playlist_list_func=list_playlists,
                    )
                    continue

                # ── 路径 mixed：混合意图 ──
                if decision.path == "mixed":
                    await ws.send_json({
                        "type": "route", "path": "mixed",
                        "reason": "混合意图，拆分子任务执行",
                    })
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
                    await _chat_handler.call_llm(
                        ws, text, "llm", auto_search=True,
                        memory=memory,
                        virtual_home=virtual_home,
                        conversation_histories=conversation_histories,
                        cancel_event=cancel_event,
                        maybe_tts_callback=_maybe_tts,
                        playlist_list_func=list_playlists,
                    )
                    continue

                # ── 路径 reasonix ──
                if decision.path == "reasonix":
                    if not is_reasonix_available():
                        logger.info("Reasonix CLI 不可用，fallback 到大模型")
                        await ws.send_json({"type": "route", "path": "llm", "reason": "Reasonix 未安装"})
                        await _chat_handler.call_llm(
                            ws, text, "llm",
                            memory=memory,
                            virtual_home=virtual_home,
                            conversation_histories=conversation_histories,
                            cancel_event=cancel_event,
                            maybe_tts_callback=_maybe_tts,
                            playlist_list_func=list_playlists,
                        )
                        continue

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

                # ── 路径 cet6：六级备考 ──
                if decision.path == "cet6":
                    # LLM 先行：文字回复 + [ACTIONS] 标签同时输出，试卷和文字一起到达
                    await _chat_handler.call_llm(
                        ws, text, "cet6",
                        memory=memory,
                        virtual_home=virtual_home,
                        conversation_histories=conversation_histories,
                        cancel_event=cancel_event,
                        maybe_tts_callback=_maybe_tts,
                        playlist_list_func=list_playlists,
                    )
                    # 兜底：小模型可能未输出 [ACTIONS] 标签，检查是否已发送试卷
                    session = get_cet6_session(id(ws))
                    if not session.get("paper_id"):
                        logger.info("CET-6 LLM 未触发试卷操作，执行兜底直接发送")
                        await _handle_cet6_route(ws, text)
                    continue

                # ── 路径 llm：大模型兜底 ──
                if decision.path == "llm":
                    await _chat_handler.call_llm(
                        ws, text, "llm",
                        memory=memory,
                        virtual_home=virtual_home,
                        conversation_histories=conversation_histories,
                        cancel_event=cancel_event,
                        maybe_tts_callback=_maybe_tts,
                        playlist_list_func=list_playlists,
                    )
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
        await ce.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=BACKEND_HOST, port=PORT, log_level="info")
