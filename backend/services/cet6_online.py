"""
CET-6 在线搜索 + 下载服务

从 https://www.wehuster.com/cet6 搜索和下载六级真题资源。
静态文件直接可下载，无需登录。

URL 模式:
  试卷 PDF:  /static/cet6/cet6_YYYY_MM_N.pdf
  听力 MP3:  /static/cet6/cet6_YYYY_MM_N.mp3
  答案 PDF:  /static/cet6/cet6_YYYY_MM_N_ans.pdf
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

WEHUSTER_BASE = "https://www.wehuster.com"
CET6_PAGE = f"{WEHUSTER_BASE}/cet6"
STATIC_BASE = f"{WEHUSTER_BASE}/static/cet6"

# 本地存放根目录
CET6_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "frontend", "public", "cet6",
)

# 在线索引缓存（启动后定时刷新）
_online_index: list[dict] | None = None
_last_fetch_time: float = 0
CACHE_TTL_SECONDS = 3600  # 1 小时


@dataclass
class OnlinePaper:
    """在线试卷信息"""
    paper_id: str          # "cet6_2021_06_1"
    year: int
    month: int
    set_num: str             # "1", "2", "3", "2-3"
    title: str
    pdf_url: str
    audio_url: str = ""
    answers_url: str = ""
    has_audio: bool = False
    has_answers: bool = False
    # 本地目录名（下载后）
    local_dir: str = ""


def _parse_paper_id(paper_id: str) -> tuple[int, int, str] | None:
    """
    解析 wehuster 试卷 ID。

    "cet6_2021_06_1"  → (2021, 6, "1")
    "cet6_2022_06_2-3" → (2022, 6, "2-3")
    """
    m = re.match(r'cet6_(\d{4})_(\d{2})_(.+)', paper_id)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.group(3)


def _paper_id_to_local_dir(paper_id: str) -> str:
    """将 wehuster ID 转为本地目录名，如 'cet6_2021_06_1' → '2021-06-1'"""
    parsed = _parse_paper_id(paper_id)
    if not parsed:
        return paper_id.replace("cet6_", "").replace("_", "-")
    year, month, set_num = parsed
    return f"{year}-{month:02d}-{set_num}"


def _paper_id_to_title(paper_id: str) -> str:
    """从 wehuster ID 生成中文标题"""
    parsed = _parse_paper_id(paper_id)
    if not parsed:
        return paper_id
    year, month, set_num = parsed
    set_cn = {"1": "第一套", "2": "第二套", "3": "第三套"}.get(set_num, f"第{set_num}套")
    return f"{year}年{month}月大学英语六级真题（{set_cn}）"


async def fetch_online_index(force: bool = False) -> list[dict]:
    """
    从 wehuster.com 抓取试卷列表。

    解析 HTML 页面中所有 /cet6/cet6_* 链接，
    提取 paper_id、构建标题和下载 URL。
    结果缓存 1 小时。
    """
    global _online_index, _last_fetch_time

    now = time.time()
    if _online_index is not None and not force and (now - _last_fetch_time) < CACHE_TTL_SECONDS:
        return _online_index

    logger.info("正在从 wehuster.com 抓取 CET-6 试卷列表...")
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(CET6_PAGE, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            html = resp.text
    except Exception as e:
        logger.warning(f"无法访问 wehuster.com: {e}")
        if _online_index is not None:
            logger.info("使用过期缓存")
            return _online_index
        return []

    # 提取所有 CET-6 试卷链接
    # 从 href 属性中提取干净的试卷 ID（避免 JSON 转义反斜杠污染）
    paper_ids: set[str] = set()
    for m in re.finditer(r'href="/cet6/(cet6_\d{4}_\d{2}_[^"_]+)"', html):
        pid = m.group(1)
        # 排除答案页面
        if pid.endswith("_ans"):
            continue
        paper_ids.add(pid)

    if not paper_ids:
        logger.warning("未从页面中提取到试卷链接")
        if _online_index is not None:
            return _online_index
        return []

    # 构建试卷信息
    index = []
    for pid in sorted(paper_ids):
        parsed = _parse_paper_id(pid)
        if not parsed:
            continue
        year, month, set_num = parsed
        local_dir = _paper_id_to_local_dir(pid)

        # 检查哪些文件实际存在于线上（HEAD 请求）
        pdf_url = f"{STATIC_BASE}/{pid}.pdf"
        audio_url = f"{STATIC_BASE}/{pid}.mp3"
        answers_url = f"{STATIC_BASE}/{pid}_ans.pdf"

        # 默认假设 PDF 和答案存在，音频待确认
        entry = {
            "paper_id": pid,
            "year": year,
            "month": month,
            "set_num": set_num,
            "title": _paper_id_to_title(pid),
            "pdf_url": pdf_url,
            "audio_url": audio_url,
            "answers_url": answers_url,
            "has_audio": False,   # 需要通过 HEAD 确认
            "has_answers": True,  # 网站通常都有答案
            "local_dir": local_dir,
            # 是否已下载到本地
            "downloaded": os.path.isfile(os.path.join(CET6_DIR, local_dir, f"{pid}.pdf")),
        }
        index.append(entry)

    _online_index = index
    _last_fetch_time = now
    logger.info(f"在线索引已更新: {len(index)} 份试卷 (2019-2025)")
    return index


def search_papers(
    year: int | None = None,
    month: int | None = None,
    set_num: str | None = None,
    exclude_downloaded: bool = True,
) -> list[dict]:
    """
    从在线索引中搜索试卷。

    Args:
        year: 年份（如 2021），None 表示不限
        month: 月份（如 6），None 表示不限
        set_num: 套号（如 "1", "2-3"），None 表示不限
        exclude_downloaded: 是否排除已下载的

    Returns:
        匹配的试卷列表
    """
    if _online_index is None:
        return []

    results = []
    for p in _online_index:
        if year is not None and p["year"] != year:
            continue
        if month is not None and p["month"] != month:
            continue
        if set_num is not None and p["set_num"] != set_num:
            continue
        if exclude_downloaded and p.get("downloaded"):
            continue
        results.append(p)

    return results


async def probe_audio(paper: dict) -> bool:
    """检测某份试卷的听力音频是否在线上存在"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.head(paper["audio_url"], headers={
                "User-Agent": "Mozilla/5.0",
            })
            return resp.status_code == 200
    except Exception:
        return False


async def download_paper(paper_id: str) -> dict | None:
    """
    下载一份试卷的 PDF、MP3（如有）、答案 PDF 到本地 cet6/ 目录。

    Returns:
        下载成功返回本地试卷信息（与 cet6_service 兼容），失败返回 None
    """
    # 确保索引已加载
    if _online_index is None:
        await fetch_online_index()

    paper = next((p for p in (_online_index or []) if p["paper_id"] == paper_id), None)
    if not paper:
        logger.warning(f"下载失败：在线索引中未找到 {paper_id}")
        return None

    local_dir = paper["local_dir"]
    local_path = os.path.join(CET6_DIR, local_dir)
    os.makedirs(local_path, exist_ok=True)

    logger.info(f"开始下载: {paper['title']} → {local_dir}/")

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        headers = {"User-Agent": "Mozilla/5.0"}

        # 1. 下载试卷 PDF
        pdf_filename = f"{paper_id}.pdf"
        pdf_path = os.path.join(local_path, pdf_filename)
        if not os.path.isfile(pdf_path):
            try:
                resp = await client.get(paper["pdf_url"], headers=headers)
                if resp.status_code == 200:
                    with open(pdf_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(f"  已下载: {pdf_filename} ({len(resp.content)} bytes)")
                else:
                    logger.warning(f"  PDF 下载失败 HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"  PDF 下载异常: {e}")

        # 2. 下载听力 MP3（尝试下载，不存在也无妨）
        mp3_filename = f"{paper_id}.mp3"
        mp3_path = os.path.join(local_path, mp3_filename)
        has_audio = False
        if not os.path.isfile(mp3_path):
            try:
                resp = await client.get(paper["audio_url"], headers=headers)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    with open(mp3_path, "wb") as f:
                        f.write(resp.content)
                    has_audio = True
                    logger.info(f"  已下载: {mp3_filename} ({len(resp.content)} bytes)")
            except Exception:
                pass
        else:
            has_audio = True

        # 3. 下载答案 PDF
        ans_filename = f"{paper_id}_ans.pdf"
        ans_path = os.path.join(local_path, ans_filename)
        has_answers = False
        if not os.path.isfile(ans_path):
            try:
                resp = await client.get(paper["answers_url"], headers=headers)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    with open(ans_path, "wb") as f:
                        f.write(resp.content)
                    has_answers = True
                    logger.info(f"  已下载: {ans_filename} ({len(resp.content)} bytes)")
            except Exception:
                pass
        else:
            has_answers = True

    # 重建本地索引
    from services.cet6_service import build_index
    build_index(force=True)

    # 标记已下载
    paper["downloaded"] = True
    paper["has_audio"] = has_audio or paper.get("has_audio", False)
    paper["has_answers"] = has_answers or paper.get("has_answers", True)

    return {
        "id": local_dir,
        "title": paper["title"],
        "has_audio": paper["has_audio"],
        "has_answers": paper["has_answers"],
        "pdf_url": f"/cet6/{local_dir}/{pdf_filename}",
        "audio_url": f"/cet6/{local_dir}/{mp3_filename}" if paper["has_audio"] else "",
        "answers_url": f"/cet6/{local_dir}/{ans_filename}" if paper["has_answers"] else "",
        "paper_id": paper_id,
        "local_dir": local_dir,
    }


def get_online_years() -> list[int]:
    """返回在线可用年份列表"""
    if not _online_index:
        return []
    return sorted(set(p["year"] for p in _online_index))


def get_online_count() -> int:
    """返回在线可用试卷总数"""
    return len(_online_index) if _online_index else 0
