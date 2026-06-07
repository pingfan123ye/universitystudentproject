"""
CET-6 备考服务：试卷索引自动扫描、随机选题、答案获取

首次启动时自动扫描 frontend/public/cet6/ 目录生成 index.json，
后续启动直接加载缓存索引，避免每次读取磁盘。

支持的文件命名（自动识别）：
  试卷 PDF：  paper.pdf  或  cet6_2025_06_1.pdf（不含 _ans 的 PDF）
  听力 MP3：  listening.mp3  或  cet6_2025_06_1.mp3
  答案 PDF：  answers.pdf  或  cet6_2025_06_1_ans.pdf（含 _ans 的 PDF）
"""
import json
import logging
import os
import random
import threading

logger = logging.getLogger(__name__)

# 试卷存放根目录 (frontend/public/cet6/)
CET6_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "frontend", "public", "cet6",
)
INDEX_PATH = os.path.join(CET6_DIR, "index.json")

_index_cache: list[dict] | None = None
_index_lock = threading.Lock()


def _dir_to_title(dir_name: str) -> str:
    """从目录名生成试卷标题，如 '2025-06-1' → '2025年6月大学英语六级真题（第一套）'"""
    parts = dir_name.split("-")
    if len(parts) >= 3:
        year, month, set_num = parts[0], parts[1], parts[2]
        set_cn = {"1": "第一套", "2": "第二套", "3": "第三套"}.get(set_num, f"第{set_num}套")
        return f"{year}年{month}月大学英语六级真题（{set_cn}）"
    return dir_name


def _scan_dir_files(dir_path: str) -> dict[str, str]:
    """
    扫描目录中的文件，自动识别试卷/听力/答案。

    Returns:
        { "paper": "cet6_2025_06_1.pdf", "audio": "cet6_2025_06_1.mp3", "answers": "cet6_2025_06_1_ans.pdf" }
        缺失的文件 key 不存在。
    """
    result: dict[str, str] = {}
    if not os.path.isdir(dir_path):
        return result

    try:
        files = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]
    except OSError:
        return result

    pdfs = [f for f in files if f.lower().endswith('.pdf')]
    mp3s = [f for f in files if f.lower().endswith('.mp3')]

    # 答案 PDF: 含 _ans 或 answers 的 PDF
    ans_pdfs = [f for f in pdfs if '_ans' in f.lower() or 'answers' in f.lower()]
    # 试卷 PDF: 不含 _ans/answers 的 PDF
    paper_pdfs = [f for f in pdfs if f not in ans_pdfs]

    if paper_pdfs:
        result["paper"] = paper_pdfs[0]
    if mp3s:
        result["audio"] = mp3s[0]
    if ans_pdfs:
        result["answers"] = ans_pdfs[0]

    return result


def _find_cet6_dirs() -> list[str]:
    """扫描 cet6/ 目录下所有包含试卷 PDF 的子目录"""
    if not os.path.isdir(CET6_DIR):
        return []
    dirs = []
    for entry in sorted(os.listdir(CET6_DIR)):
        entry_path = os.path.join(CET6_DIR, entry)
        if os.path.isdir(entry_path):
            scanned = _scan_dir_files(entry_path)
            if "paper" in scanned:
                dirs.append(entry)
            else:
                logger.debug(f"CET-6 目录 '{entry}' 缺少试卷 PDF，跳过")
    return dirs


def build_index(force: bool = False) -> list[dict]:
    """
    扫描 cet6/ 目录并生成/更新试卷索引。

    Returns:
        试卷列表，每项: {id, title, has_audio, has_answers,
                        pdf_url, audio_url, answers_url,
                        paper_file, audio_file, answers_file}
    """
    global _index_cache

    with _index_lock:
        # 有缓存直接返回
        if _index_cache is not None and not force:
            return _index_cache

        # 已有 index.json 且不强制重建 → 加载
        if os.path.isfile(INDEX_PATH) and not force:
            try:
                with open(INDEX_PATH, encoding="utf-8") as f:
                    _index_cache = json.load(f)
                logger.info(f"CET-6 索引已加载: {len(_index_cache)} 份试卷")
                return _index_cache
            except Exception as e:
                logger.warning(f"CET-6 索引加载失败，将重新扫描: {e}")

        # 扫描目录生成索引
        paper_dirs = _find_cet6_dirs()
        if not paper_dirs:
            logger.info("CET-6 目录为空或无有效试卷，跳过索引生成")
            _index_cache = []
            return _index_cache

        index = []
        for dir_name in paper_dirs:
            dir_path = os.path.join(CET6_DIR, dir_name)
            scanned = _scan_dir_files(dir_path)
            has_audio = "audio" in scanned
            has_answers = "answers" in scanned
            title = _dir_to_title(dir_name)

            paper_file = scanned.get("paper", "")
            audio_file = scanned.get("audio", "")
            answers_file = scanned.get("answers", "")

            entry = {
                "id": dir_name,
                "title": title,
                "has_audio": has_audio,
                "has_answers": has_answers,
                "pdf_url": f"/cet6/{dir_name}/{paper_file}",
                "audio_url": f"/cet6/{dir_name}/{audio_file}" if has_audio else "",
                "answers_url": f"/cet6/{dir_name}/{answers_file}" if has_answers else "",
                "_paper_file": paper_file,
                "_audio_file": audio_file,
                "_answers_file": answers_file,
            }
            index.append(entry)

        # 写入 index.json
        try:
            os.makedirs(CET6_DIR, exist_ok=True)
            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
            logger.info(f"CET-6 索引已生成: {INDEX_PATH} ({len(index)} 份试卷)")
        except Exception as e:
            logger.warning(f"CET-6 索引写入失败: {e}")

        _index_cache = index
        return index


def _load_index() -> list[dict]:
    """加载试卷索引（优先缓存，回退扫描）"""
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    return build_index()


def select_random_paper(year=None, month=None, set_num=None) -> dict | None:
    """
    从索引中随机选一份试卷，支持按年份/月份/套号过滤。

    Args:
        year:  年份过滤（如 2025），None 表示不限
        month: 月份过滤（如 6），None 表示不限
        set_num: 套号过滤（如 1），None 表示不限

    Returns:
        匹配的试卷 dict，无匹配时返回 None（调用方可 fallback 到在线搜索）
    """
    index = _load_index()
    if not index:
        return None

    candidates = []
    for p in index:
        pid = p.get("id", "")
        parts = pid.split("-")
        if len(parts) < 3:
            # 非标准 ID 格式 → 仍然纳入候选（不丢试卷）
            candidates.append(p)
            continue
        if year is not None and parts[0] != str(year):
            continue
        if month is not None and parts[1] != str(month).zfill(2):
            continue
        if set_num is not None and parts[2] != str(set_num):
            continue
        candidates.append(p)

    if not candidates:
        return None
    return random.choice(candidates)


def get_answers(paper_id: str) -> dict:
    """根据 paper_id 返回答案 PDF 的 URL"""
    index = _load_index()
    paper = next((p for p in index if p["id"] == paper_id), None)
    if paper and paper.get("has_answers"):
        return {"pdf_url": paper["answers_url"], "paper_id": paper_id}
    # 回退：直接扫描目录
    dir_path = os.path.join(CET6_DIR, paper_id)
    scanned = _scan_dir_files(dir_path)
    if "answers" in scanned:
        return {"pdf_url": f"/cet6/{paper_id}/{scanned['answers']}", "paper_id": paper_id}
    return {"error": "答案文件不存在"}


def get_paper_count() -> int:
    """返回可用试卷数量"""
    return len(_load_index())


# ═══════════════════════════════════════════════════════════
# CET-6 会话状态管理
# ═══════════════════════════════════════════════════════════

# key: id(websocket), value: {
#     "paper_id": str,              # 当前活跃试卷
#     "search_results": list[dict],  # 最近在线搜索结果
# }
_cet6_sessions: dict[int, dict] = {}


def get_cet6_session(session_id: int) -> dict:
    """获取或创建 CET-6 会话状态"""
    if session_id not in _cet6_sessions:
        _cet6_sessions[session_id] = {}
    return _cet6_sessions[session_id]


# ═══════════════════════════════════════════════════════════
# CET-6 试卷发送
# ═══════════════════════════════════════════════════════════

async def send_cet6_paper(ws, paper: dict, tts_text: str | None = None, tts_callback=None):
    """发送 CET-6 试卷到前端（含 PDF 附件气泡 + 听力自动播放）

    tts_callback: async (ws, text, path) — TTS 播报回调，避免循环导入
    """
    await ws.send_json({
        "type": "cet6_paper",
        "paper_id": paper["id"],
        "title": paper["title"],
        "pdf_url": paper["pdf_url"],
        "has_audio": paper.get("has_audio", False),
        "audio_url": paper.get("audio_url", ""),
        "has_answers": paper.get("has_answers", False),
        "answers_url": paper.get("answers_url", ""),
    })
    await ws.send_json({
        "type": "chat_attachment",
        "label": f"📎 下载 {paper['title']} 真题",
        "url": paper["pdf_url"],
    })
    # 听力自动播放
    if paper.get("has_audio") and paper.get("audio_url"):
        await ws.send_json({
            "type": "music_control", "action": "play",
            "song_name": paper["title"] + " 听力音频",
            "download_url": paper["audio_url"],
            "source": "local",
        })
        logger.info(f"CET-6 听力播放: {paper['title']}")
    # TTS 语音播报
    if tts_text and tts_callback:
        import asyncio
        asyncio.create_task(tts_callback(ws, tts_text, "cet6"))


# ═══════════════════════════════════════════════════════════
# CET-6 操作执行
# ═══════════════════════════════════════════════════════════

async def handle_cet6_action(ws, action: dict, session_id: int, tts_callback=None):
    """执行 LLM 通过 [ACTIONS]{{"cet6":{{...}}}} 输出的 CET-6 操作。

    与音乐/设备控制完全一致的模式：LLM 自主决定何时调用 CET-6 功能，
    后端负责执行具体的试卷查找、听力播放、答案获取等操作。

    tts_callback: async (ws, text, path) — TTS 播报回调，避免循环导入
    """
    from services.cet6_online import (
        fetch_online_index, search_papers, download_paper, get_online_count,
    )

    act_type = action.get("action", "")
    state = get_cet6_session(session_id)

    if act_type == "random_paper":
        year = action.get("year")
        month = action.get("month")
        set_num = action.get("set")
        paper = select_random_paper(year=year, month=month, set_num=set_num)
        if paper:
            _cet6_sessions[session_id] = {"paper_id": paper["id"]}
            await send_cet6_paper(ws, paper,
                tts_text=f"好的，为你准备了{paper['title']}，先做听力部分吧",
                tts_callback=tts_callback)
            logger.info(f"CET-6 [ACTIONS] random_paper: {paper['title']} (year={year}, month={month}, set={set_num})")
        else:
            year_str = f"{year}年" if year else ""
            month_str = f"{month}月" if month else ""
            logger.info(f"CET-6 [ACTIONS] random_paper 本地无匹配 (year={year}, month={month})，转在线搜索")
            await fetch_online_index()
            results = search_papers(year=year, month=month)
            if results:
                state["search_results"] = results
                _cet6_sessions[session_id] = state
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
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": f"本地没有{year_str}{month_str}的真题，但在线找到了 {len(results)} 套，要下载哪一套？",
                    "model": "cet6",
                })
            else:
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": f"抱歉，本地和在线都没有找到{year_str}{month_str}的六级真题，你可以换个年份试试",
                    "model": "cet6",
                })

    elif act_type == "paper":
        year = action.get("year")
        month = action.get("month")
        set_num = action.get("set")
        papers = _load_index()
        matched = None
        for p in papers:
            pid = p.get("id", "")
            parts = pid.split("-")
            if len(parts) >= 3:
                if year is not None and parts[0] != str(year):
                    continue
                if month is not None and parts[1] != str(month).zfill(2):
                    continue
                if set_num is not None and parts[2] != str(set_num):
                    continue
                matched = p
                break
        if matched:
            _cet6_sessions[session_id] = {"paper_id": matched["id"]}
            await send_cet6_paper(ws, matched,
                tts_text=f"好的，为你找到{matched['title']}，开始练习吧",
                tts_callback=tts_callback)
            logger.info(f"CET-6 [ACTIONS] paper: {matched['title']}")
        else:
            logger.info(f"CET-6 [ACTIONS] paper 本地未匹配 year={year} month={month}，转在线搜索")
            await fetch_online_index()
            results = search_papers(year=year, month=month)
            if results:
                state["search_results"] = results
                _cet6_sessions[session_id] = state
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
                year_str = f"{year}年" if year else "相关"
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": f"本地没有{year_str}的真题，但在线找到了 {len(results)} 套，要下载哪一套？",
                    "model": "cet6",
                })
            else:
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": f"抱歉，本地和在线都没有找到{'{}年'.format(year) if year else '相关'}的六级真题",
                    "model": "cet6",
                })

    elif act_type == "browse":
        papers = _load_index()
        if papers:
            local_results = []
            for p in papers:
                pid = p.get("id", "")
                pid_parts = pid.split("-") if "-" in pid else []
                local_results.append({
                    "paper_id": pid,
                    "title": p["title"],
                    "year": pid_parts[0] if len(pid_parts) >= 1 else "",
                    "month": pid_parts[1] if len(pid_parts) >= 2 else "",
                    "set_num": pid_parts[2] if len(pid_parts) >= 3 else "",
                    "downloaded": True,
                })
            await ws.send_json({
                "type": "cet6_search_results",
                "results": local_results,
            })
            lines = []
            for i, p in enumerate(papers, 1):
                audio_tag = "🎧" if p.get("has_audio") else ""
                ans_tag = "📝" if p.get("has_answers") else ""
                lines.append(f"{i}. {p['title']} {audio_tag}{ans_tag}")
            summary = "当前题库有以下真题：\n" + "\n".join(lines) + "\n\n告诉我你要做哪一套，或者说「做第一套」"
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": summary,
                "model": "cet6",
            })
            logger.info(f"CET-6 [ACTIONS] browse: {len(papers)} 套本地试卷")
        else:
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": "本地题库暂时为空，你可以说「联网找真题」来搜索下载",
                "model": "cet6",
            })

    elif act_type == "search":
        year = action.get("year")
        await fetch_online_index()
        results = search_papers(year=year)
        if results:
            state["search_results"] = results
            _cet6_sessions[session_id] = state
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
            years = sorted(set(r["year"] for r in results))
            year_str = f"{years[0]}年" if len(years) == 1 else f"{years[0]}-{years[-1]}年"
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": f"找到 {len(results)} 套{year_str}的六级真题，要下载哪一套？",
                "model": "cet6",
            })
            logger.info(f"CET-6 [ACTIONS] search: {len(results)} 套在线试卷")
        else:
            online_count = get_online_count()
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": f"在线题库中未找到匹配的试卷（目前有 {online_count} 套在线试卷）",
                "model": "cet6",
            })

    elif act_type == "answers":
        paper_id = state.get("paper_id")
        if paper_id:
            answers = get_answers(paper_id)
            await ws.send_json({
                "type": "cet6_answers",
                "paper_id": paper_id,
                "pdf_url": answers.get("pdf_url", ""),
                "error": answers.get("error", ""),
            })
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": "已展示答案，请核对" if "pdf_url" in answers else "抱歉，这份试卷暂无答案解析",
                "model": "cet6",
            })
            logger.info(f"CET-6 [ACTIONS] answers: paper={paper_id}")
        else:
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": "当前没有活跃的试卷，请先说「做真题」来选择一套",
                "model": "cet6",
            })

    elif act_type == "listening":
        paper_id = state.get("paper_id")
        if paper_id:
            papers = _load_index()
            paper = next((p for p in papers if p["id"] == paper_id), None)
            if paper and paper.get("has_audio") and paper.get("audio_url"):
                await ws.send_json({
                    "type": "music_control", "action": "play",
                    "song_name": paper["title"] + " 听力音频",
                    "download_url": paper["audio_url"],
                    "source": "local",
                })
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": f"正在播放 {paper['title']} 的听力音频 🎧",
                    "model": "cet6",
                })
                logger.info(f"CET-6 [ACTIONS] listening: {paper['title']}")
            else:
                await ws.send_json({
                    "type": "done", "path": "cet6",
                    "reply": "当前试卷没有听力音频文件",
                    "model": "cet6",
                })
        else:
            await ws.send_json({
                "type": "done", "path": "cet6",
                "reply": "当前没有活跃的试卷，请先说「做真题」来选择一套",
                "model": "cet6",
            })

    else:
        logger.warning(f"CET-6 未知 action: {act_type}")
