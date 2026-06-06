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
