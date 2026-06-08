"""
离线语音转写服务 —— 讯飞 IAT（语音听写）API
需要先到 https://www.xfyun.cn/service/voicedictation 申请 AppID / APIKey / APISecret
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ── 从统一配置加载 ──
from config import IFT_APPID as _APPID, IFT_API_KEY as _API_KEY, IFT_API_SECRET as _API_SECRET

if not _APPID or _APPID in ("你的APPID", "your_appid"):
    logger.info("讯飞 API 未配置（IFT_APPID 未设置），云端 STT fallback 不可用")

_HOST = "iat-api.xfyun.cn"
_PATH = "/v2/iat"
_URL = f"wss://{_HOST}{_PATH}"

FRAME_BYTES = 1280  # 每帧 80ms @ 16kHz 16bit mono = 1280 bytes


def _build_auth_url() -> Optional[str]:
    """构建带 HMAC-SHA256 签名的 WebSocket URL"""
    if not _APPID or not _API_KEY or not _API_SECRET:
        return None
    if _APPID == "你的APPID":
        logger.warning("讯飞 API 配置尚未填写")
        return None

    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    signature_origin = f"host: {_HOST}\ndate: {date}\nGET {_PATH} HTTP/1.1"
    signature_sha = hmac.new(
        _API_SECRET.encode(), signature_origin.encode(), digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_sha).decode()
    authorization_origin = (
        f'api_key="{_API_KEY}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()
    params = urlencode({"authorization": authorization, "date": date, "host": _HOST})
    return f"{_URL}?{params}"


async def transcribe_audio_base64(audio_b64: str) -> str:
    """将 base64 PCM16 音频发到讯飞 IAT API 转写为文字"""
    if not audio_b64:
        return ""

    ws_url = _build_auth_url()
    if not ws_url:
        return ""

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception as e:
        logger.warning("base64 解码失败: %s", e)
        return ""

    # 前端发送的是 WAV 格式（含 44 字节 RIFF 头），讯飞 API 要求 raw PCM 16kHz 16bit
    # 检测并剥离 WAV 头，保留纯 PCM 数据
    if len(audio_bytes) > 44 and audio_bytes[:4] == b'RIFF':
        audio_bytes = audio_bytes[44:]

    if len(audio_bytes) < 1024:
        logger.warning("音频过短 (%d bytes)，跳过转写", len(audio_bytes))
        return ""

    try:
        import websockets
    except ImportError:
        logger.error("websockets 未安装")
        return ""

    text_parts: list[str] = []

    try:
        async with websockets.connect(ws_url, ping_interval=None) as ws:
            # 分帧发送
            total = len(audio_bytes)
            for i in range(0, total, FRAME_BYTES):
                chunk = audio_bytes[i:i + FRAME_BYTES]
                chunk_b64 = base64.b64encode(chunk).decode()
                is_first = i == 0
                is_last = i + FRAME_BYTES >= total

                status = 2 if is_last else (0 if is_first else 1)
                msg = {"data": {"status": status, "format": "audio/L16;rate=16000", "encoding": "raw", "audio": chunk_b64}}
                if is_first:
                    msg["common"] = {"app_id": _APPID}
                    msg["business"] = {"language": "zh_cn", "domain": "iat", "accent": "mandarin", "vad_eos": 3000, "dwa": "wpgs"}
                await ws.send(json.dumps(msg))

            # 接收结果
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30)
                if isinstance(msg, bytes):
                    continue
                data = json.loads(msg)
                code = data.get("code", 0)
                if code != 0:
                    logger.warning("讯飞 API 错误: %s — %s", code, data.get("message", ""))
                    break
                if data.get("data", {}).get("result", {}).get("ws"):
                    for ws_item in data["data"]["result"]["ws"]:
                        for cw in ws_item.get("cw", []):
                            w = cw.get("w", "")
                            if w:
                                text_parts.append(w)
                if data.get("data", {}).get("status") == 2:
                    break

    except asyncio.TimeoutError:
        logger.warning("讯飞 IAT 超时")
    except Exception as e:
        logger.error("讯飞 IAT 转写出错: %s", e)
        return ""

    result = "".join(text_parts).strip()
    if result:
        logger.info("讯飞转写结果 (%d chars): %s", len(result), result[:60])
    return result


async def is_available() -> bool:
    if not _APPID or _APPID == "你的APPID":
        return False
    try:
        import websockets
        return True
    except ImportError:
        return False
