"""
聊天消息核心处理 —— LLM 调用、缓存、记忆提取、路由分发
"""
import asyncio
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# 上次 LLM 生成开始时间（供 main.py 唤醒词防抖使用）
_last_gen_started_at: float = 0.0

from services.llm_service import generate_stream, parse_actions, _strip_actions_tags, strip_search_tags, polish_local_reply
from services.music_service import send_music_control, _user_requested_music
from services.cache_engine import get_cache, is_low_quality_reply


async def call_llm(
    ws,
    prompt: str,
    route_path: str = "llm",
    *,
    auto_search: bool = False,
    prefer_cloud: bool = False,
    extra_context: str = "",
    memory,
    virtual_home,
    conversation_histories: dict,
    cancel_event: asyncio.Event,
    maybe_tts_callback,
    playlist_list_func,
):
    """
    调用 LLM 生成回复（含缓存、搜索、ACTIONS 执行、记忆提取）。

    此函数从 main.py 的嵌套 call_llm 提取而来。
    """
    from services.intent_router import classify
    from services.xiaoai_service import execute as xiaoai_execute
    from services.search_service import search_to_context
    from services.cet6_service import handle_cet6_action
    from services.stt_corrector import is_low_quality_stt, _is_hallucination

    cache = get_cache()

    def _get_history(path: str) -> list:
        key = path if path in ("cet6", "music") else "llm"
        return conversation_histories[key]

    # ── 0. 查缓存 ──
    cached = cache.check_and_get(prompt)
    if cached:
        logger.info(f"缓存命中: {prompt[:30]}...")
        reply_text = cached["reply"]
        clean_reply, cached_actions = parse_actions(reply_text)
        if cached_actions:
            reply_text = clean_reply
        else:
            actions_json = cached.get("actions_json")
            if actions_json:
                try:
                    cached_actions = json.loads(actions_json)
                except json.JSONDecodeError:
                    logger.warning(f"缓存 actions_json 解析失败: {actions_json[:100]}")

        reply_text = _strip_actions_tags(reply_text)
        await ws.send_json({"type": "token", "text": reply_text})
        await ws.send_json({"type": "done", "path": "cache", "reply": reply_text, "model": "cache"})
        asyncio.create_task(maybe_tts_callback(ws, reply_text, "cache"))

        if cached_actions:
            music_act = cached_actions.get("music")
            device_acts = cached_actions.get("devices", [])
            if music_act and isinstance(music_act, dict):
                if _user_requested_music(prompt):
                    await send_music_control(ws, music_act, tts_callback=maybe_tts_callback)
                    logger.info(f"[缓存] 执行音乐: {music_act}")
                else:
                    logger.warning(f"[缓存] 跳过幻觉音乐标签: {json.dumps(music_act, ensure_ascii=False)[:120]}")
            if device_acts:
                for da in device_acts:
                    virtual_home.execute(da.get("device", ""), da.get("action", "toggle"))
                await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
        return

    # ── 0.5 构建上下文 ──
    mem_ctx = memory.get_context()
    if extra_context:
        mem_ctx = extra_context + "\n" + mem_ctx if mem_ctx else extra_context

    # 注入歌单列表
    try:
        playlists_available = playlist_list_func()
        if playlists_available:
            names = "、".join(playlists_available.keys())
            mem_ctx += (
                f"\n\n【当前可用歌单 — 必须原样使用】{names}\n"
                "规则：当用户请求播放音乐且未指定具体歌名时，你必须从上面列表中**原样复制**一个歌单名。\n"
                "不确定选哪个时：学习/专注/焦虑/助眠→优先选含'轻音乐'的；日常/随便→优先选'收藏'。\n"
                "**绝对不要**自己编造歌单名，不确定就留空 playlist 让后端自己选。"
            )
    except Exception:
        pass

    # ── 自动搜索 ──
    if auto_search:
        logger.info(f"自动搜索: {prompt[:40]}...")
        await ws.send_json({"type": "search_status", "status": "searching", "message": "正在联网搜索..."})
        search_ctx = await search_to_context(prompt)
        if search_ctx:
            mem_ctx += "\n" + search_ctx
            await ws.send_json({
                "type": "search_status", "status": "done",
                "message": "已获取搜索结果",
                "result": search_ctx[:200] + ("..." if len(search_ctx) > 200 else ""),
            })

    # ── 1. 调用 LLM ──
    global _last_gen_started_at
    model_used: list[str] = []
    actions_out: list[str] = []
    full_reply = ""
    cancel_event.clear()
    _last_gen_started_at = time.monotonic()  # 记录生成开始时间（唤醒词防抖）

    from services.tts_service import clean_for_tts

    # ★ 句子级流式 TTS：累积 token，检测到句末标点立即合成
    sentence_buffer = ""
    tts_seq = 0

    def _extract_sentence(buf: str) -> tuple[str | None, str]:
        """从 buffer 中提取第一句完整句子。返回 (句子, 剩余) 或 (None, buf)"""
        earliest = -1
        for punct in ['。', '！', '？']:
            idx = buf.find(punct)
            if idx >= 0 and (earliest < 0 or idx < earliest):
                earliest = idx
        if earliest >= 0:
            punct = buf[earliest]
            return buf[:earliest + 1].strip(), buf[earliest + 1:]
        return None, buf

    async def _flush_tts(text: str):
        """异步发送单句 TTS，序列号递增"""
        nonlocal tts_seq
        tts_text = clean_for_tts(text)
        if tts_text and len(tts_text) >= 2:
            seq = tts_seq
            tts_seq += 1
            asyncio.create_task(maybe_tts_callback(ws, tts_text, route_path, seq))

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
            sentence_buffer += token
            # 检测句子边界，每检测到一句就异步 TTS
            while True:
                sentence, sentence_buffer = _extract_sentence(sentence_buffer)
                if sentence is None:
                    break
                await _flush_tts(sentence)
    except RuntimeError as e:
        full_reply = strip_search_tags(full_reply)
        logger.error(f"大模型调用失败: {e}")
        await ws.send_json({"type": "error", "error": str(e)})
        await ws.send_json({
            "type": "done", "path": route_path, "reply": full_reply,
            "model": model_used[0] if model_used else "unknown",
        })
        if tts_seq == 0:
            await maybe_tts_callback(ws, full_reply, route_path)
        return

    if cancel_event.is_set():
        logger.info("LLM 流被取消（唤醒词打断）")
        await ws.send_json({"type": "cancelled"})
        return

    # ── 2. 处理 ACTIONS 标签 ──

    llm_actions = None
    for action_text in actions_out:
        _, actions = parse_actions(action_text)
        if actions:
            llm_actions = actions
            break
    if not llm_actions and '[ACTIONS]' in full_reply:
        full_reply, llm_actions = parse_actions(full_reply)

    full_reply = strip_search_tags(full_reply)
    display_reply = _strip_actions_tags(full_reply)
    actual_model = model_used[0] if model_used else "unknown"

    # ★ 本地小模型后处理：清洗模板残渣（前缀/后缀/重复句）
    if actual_model.startswith("ollama:") or actual_model.startswith("local:"):
        polished = polish_local_reply(display_reply.strip())
        if polished != display_reply.strip():
            logger.info(f"本地模型输出已清洗: {display_reply.strip()[:50]}... → {polished[:50]}...")
            display_reply = polished
            full_reply = polished  # 同步更新，后续缓存/历史使用清洗后的版本

    await ws.send_json({
        "type": "done", "path": route_path,
        "reply": display_reply.strip(), "model": actual_model,
    })

    # ★ TTS：如果流式过程中已逐句发送，只需发送剩余 buffer；否则发送全文
    if tts_seq > 0:
        # 流式 TTS 已覆盖完整句子，处理剩余碎片（无句末标点的结尾）
        if sentence_buffer.strip():
            await _flush_tts(sentence_buffer.strip())
    else:
        # 无句子边界（极短回复或纯标签），发送全文
        tts_reply = clean_for_tts(display_reply)
        asyncio.create_task(maybe_tts_callback(ws, tts_reply.strip(), route_path, 0))

    # ── 3. 执行 ACTIONS ──
    if llm_actions:
        music_act = llm_actions.get("music")
        device_acts = llm_actions.get("devices", [])
        if music_act and isinstance(music_act, dict):
            if _user_requested_music(prompt):
                await send_music_control(ws, music_act, tts_callback=maybe_tts_callback)
            else:
                logger.warning(f"[ACTIONS] 跳过幻觉音乐标签: {json.dumps(music_act, ensure_ascii=False)[:120]}")
        if device_acts:
            valid_devices = {"bedroom_light", "living_light", "kitchen_light", "bathroom_light",
                           "study_light", "living_curtain", "ac", "water_heater"}
            for da in device_acts:
                dev_id = da.get("device", "")
                if dev_id not in valid_devices:
                    logger.warning(f"[ACTIONS] 拒绝未知设备 ID: {dev_id} (LLM 虚构)")
                    continue
                virtual_home.execute(dev_id, da.get("action", "toggle"))
            await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})
        cet6_act = llm_actions.get("cet6")
        if cet6_act and isinstance(cet6_act, dict):
            await handle_cet6_action(ws, cet6_act, id(ws), tts_callback=maybe_tts_callback)

    # ── 4. 兜底音乐检测 ──
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
            playlist_name = ""
            try:
                available = playlist_list_func()
                for pname in available:
                    if pname in full_reply:
                        playlist_name = pname
                        break
                if not playlist_name and available:
                    prompt_lower = prompt.lower()
                    for pname in available:
                        if any(kw in prompt_lower for kw in ["学习", "专注", "轻音乐", "安静", "助眠"]):
                            if "轻音乐" in pname:
                                playlist_name = pname
                                break
                    if not playlist_name:
                        playlist_name = list(available.keys())[0]
            except Exception:
                pass
            await send_music_control(ws, {"action": "play", "playlist": playlist_name}, tts_callback=maybe_tts_callback)

    # ── 5. 更新对话历史 ──
    reply_text = full_reply.strip()
    if reply_text:
        history = _get_history(route_path)
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply_text})
        if len(history) > 10:
            history[:2] = []

    # ── 6. 用户原文隐含意图提取 ──
    if route_path == "llm" and full_reply.strip():
        implicit = classify(prompt)
        if implicit.path == "xiaoai" and (implicit.device_actions or implicit.music_action):
            if implicit.music_action:
                q = implicit.music_action.get("query", "")
                if q and (len(q) > 30 or any(p in q for p in ["。", "！", "？", "，", "希望", "如果", "推荐", "告诉", "比如", "或", "之类"])):
                    logger.info(f"用户原文音乐查询无效({q[:40]})，清空 query")
                    implicit.music_action["query"] = ""
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
                        await send_music_control(ws, implicit_result["music_action"], tts_callback=maybe_tts_callback)
                    if implicit_result["results"]:
                        await ws.send_json({"type": "device_state", "devices": virtual_home.get_all_states()})

    # ── 7. 记忆提取 ──
    if route_path == "llm" and full_reply.strip():
        if not is_low_quality_stt(prompt) and not _is_hallucination(prompt):
            new_memories = memory.extract_and_store(prompt)
        else:
            new_memories = []
        if new_memories:
            logger.info(f"新记忆（正则）: {new_memories}")
            await ws.send_json({
                "type": "memory_learned",
                "memories": new_memories,
                "message": "我记住了关于你的新信息",
            })
        # ★ LLM 提取成功后通知前端（异步不阻塞）
        async def _extract_and_notify():
            results = await memory.extract_with_llm(prompt, full_reply.strip())
            if results:
                await ws.send_json({
                    "type": "memory_learned",
                    "memories": results,
                    "message": "我记住了关于你的新信息",
                })

        asyncio.create_task(_extract_and_notify())

    # ── 8. 缓存学习（跳过噪音输入 + 低质量回复，防止模板/困惑回复污染缓存）──
    if route_path == "llm" and full_reply.strip():
        _is_noise = (
            len(prompt.strip()) < 5
            and not any('一' <= c <= '鿿' for c in prompt)
        )
        _low_quality = is_low_quality_reply(full_reply.strip())
        if _low_quality:
            logger.info(f"LLM 计数: 跳过低质量回复缓存: {full_reply.strip()[:50]}...")
        if not _is_noise and not _low_quality:
            count, reached = cache.increment_and_check(prompt)
            logger.info(f"LLM 计数: {prompt[:30]}... -> {count}/3")
            if reached:
                actions_json = json.dumps(llm_actions, ensure_ascii=False) if llm_actions else None
                cache.store_reply(prompt, full_reply.strip(), actions_json=actions_json)
                await ws.send_json({
                    "type": "cache_learned",
                    "text": prompt[:50],
                    "message": "我记住了这个对话习惯，下次可以直接回答",
                })
        elif _is_noise:
            logger.info(f"LLM 计数: 跳过噪音输入缓存: {prompt[:30]}...")
