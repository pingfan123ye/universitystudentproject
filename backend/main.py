"""
智能音箱_无实物 —— 后端服务入口 (v0.2.0)
FastAPI + WebSocket 实现 AI 语音交互中枢（三道路由分发）
"""
import json
import logging
import os
import re
import socket
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from services.intent_router import classify
from services.llm_service import generate_stream, check_model_available, DEFAULT_MODEL
from services.xiaoai_service import execute as xiaoai_execute
from services.reasonix_executor import execute as reasonix_execute, is_reasonix_available, get_pending_manager
from services.cache_engine import get_cache
from services.memory_engine import get_memory
from models.virtual_home import VirtualHome

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ===== 强制清理旧进程 =====
PORT = 8000


def _kill_old_process():
    """杀掉占用目标端口的旧进程，确保新代码能启动"""
    if sys.platform != "win32":
        return
    try:
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
            time.sleep(1)  # 等一下让 OS 释放端口
            print("[启动] 旧进程已清理，端口已释放")
        else:
            print(f"[启动] 端口 {PORT} 空闲")
    except Exception as e:
        print(f"[启动] 清理旧进程时出错: {e}")


_kill_old_process()

# ===== 全局虚拟家庭实例 =====
virtual_home = VirtualHome()
APP_VERSION = "0.2.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    banner = f"""
========================================
  智能音箱后端 v{APP_VERSION}
  三道路由: 小爱 | 大模型 | Reasonix
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
    logger.info("三道路由分发已启用")
    yield


app = FastAPI(title="智能音箱_无实物 API", version="0.2.0", lifespan=lifespan)


@app.get("/health")
async def health_check():
    model_ok = await check_model_available()
    return JSONResponse({
        "status": "ok",
        "version": APP_VERSION,
        "model_available": model_ok,
        "model": DEFAULT_MODEL,
        "reasonix_available": is_reasonix_available(),
        "router": "xiaoai | llm | reasonix",
    })


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

    # 对话历史（跨轮记忆，最多保留最近 20 条消息 = 10 轮）
    conversation_history: list[dict] = []

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
                    "好的", "好", "好啊", "好吧", "好呀", "好嘞",       # 好字辈
                    "来", "来吧", "上", "上吧",                        # 来/上
                    "做吧", "弄吧", "干吧",                             # 干活
                    "确定", "确认",                                     # 确认
                    "就这样", "就这么办",                               # 就这样
                    "ok", "OK", "okay", "go", "yes",                   # 英文
                }
                _clean = re.sub(r'[\s,，。！？、；：""''《》!?;:\'()\u3000]+', '', text)
                # 精确匹配 或 短句（≤6字）前缀命中
                _matched = _clean in APPROVAL_WORDS or (len(_clean) <= 6 and any(_clean.startswith(w) for w in APPROVAL_WORDS if len(w) <= len(_clean)))
                if _matched:
                    pm = get_pending_manager()
                    task = pm.pop_next()
                    if task:
                        logger.info(f"Reasonix 任务已批准: {task.prompt[:50]}...")
                        await ws.send_json({
                            "type": "route", "path": "reasonix", "reason": "Reasonix已批准，开始执行",
                        })
                        full_output = ""
                        async for line in reasonix_execute(task.prompt, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
                            await ws.send_json({"type": "token", "text": line})
                            full_output += line
                        await ws.send_json({"type": "done", "path": "reasonix", "reply": full_output.strip()})
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

                async def call_llm(prompt: str, route_path: str = "llm"):
                    # 0. 查缓存
                    cached = cache.check_and_get(prompt)
                    if cached:
                        logger.info(f"缓存命中: {prompt[:30]}...")
                        await ws.send_json({"type": "token", "text": cached["reply"]})
                        await ws.send_json({"type": "done", "path": "cache", "reply": cached["reply"]})
                        return

                    # 1. 调大模型（注入用户记忆）
                    mem_ctx = memory.get_context()
                    full_reply = ""
                    try:
                        async for token in generate_stream(prompt, memory_context=mem_ctx, conversation_history=conversation_history):
                            await ws.send_json({"type": "token", "text": token})
                            full_reply += token
                    except RuntimeError as e:
                        logger.error(f"大模型调用失败: {e}")
                        await ws.send_json({"type": "error", "error": str(e)})
                        await ws.send_json({"type": "done", "path": route_path, "reply": full_reply})
                        return

                    await ws.send_json({"type": "done", "path": route_path, "reply": full_reply.strip()})

                    # 5. 更新对话历史（非缓存路径）
                    reply_text = full_reply.strip()
                    if reply_text:
                        conversation_history.append({"role": "user", "content": prompt})
                        conversation_history.append({"role": "assistant", "content": reply_text})
                        # 限制最大对话轮数（最近 10 轮 = 20 条消息）
                        if len(conversation_history) > 20:
                            conversation_history[:2] = []  # 裁掉最早的一轮（user+assistant）

                    # 2. 大模型回复后：从「用户原文 + LLM回复」两端提取设备/音乐操作
                    #    这样即使用户说"我想放松一下"（无关键词），大模型回复中"帮你放歌开灯"也会触发真实执行
                    if route_path == "llm" and full_reply.strip():
                        implicit = classify(prompt)
                        # 如果用户原文没提取到，从 LLM 回复中再试
                        if implicit.path != "xiaoai" or (not implicit.device_actions and not implicit.music_action):
                            from_reply = classify(full_reply.strip())
                            if from_reply.path == "xiaoai" and (from_reply.device_actions or from_reply.music_action):
                                implicit = from_reply
                                logger.info(f"从LLM回复中提取到操作: {full_reply[:60]}...")

                        if implicit.path == "xiaoai" and (implicit.device_actions or implicit.music_action):
                            implicit_result = xiaoai_execute(
                                virtual_home=virtual_home,
                                device_actions=implicit.device_actions,
                                text=prompt,
                                matched_key=implicit.matched_key,
                                music_action=implicit.music_action,
                            )
                            if implicit_result["handled"]:
                                if implicit_result.get("music_action"):
                                    await ws.send_json({"type": "music_control", "action": implicit_result["music_action"]["action"]})
                                if implicit_result["results"]:
                                    await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                                logger.info(f"LLM回复后自动执行: devices={implicit.device_actions} music={implicit.music_action}")

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

                # ===== 路径 A：小爱先尝试 =====
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
                        await ws.send_json({"type": "done", "path": "xiaoai", "reply": result["reply"]})
                        if result.get("music_action"):
                            await ws.send_json({"type": "music_control", "action": result["music_action"]["action"]})
                        if result["results"]:
                            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
                        continue
                    else:
                        # 小爱无法处理 → 直接调大模型
                        logger.info(f"小爱无法处理，直接调用大模型: {text[:50]}")
                        await ws.send_json({"type": "route", "path": "llm", "reason": "小爱无法处理，转交大模型"})
                        await call_llm(text, "llm")
                        continue

                # ===== 路径 B：Reasonix 执行（需审批） =====
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
