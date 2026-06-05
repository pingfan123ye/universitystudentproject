"""
高频缓存引擎 —— 内存 LRU + SQLite 双层
仅对大模型路径的指令计数和缓存，设备路径不参与。
"""
import hashlib
import re
import sqlite3
import time
from collections import OrderedDict


# ===== 指令标准化 =====
FILLER_WORDS = re.compile(r'[吧啊呢嘛呀哦嗯哈]')
PUNCTUATION = re.compile(r'[\s，。！？、；：""''（）《》,.!?;:\"\'\)\(]+')

# 同义词映射（缩短后统一）
SYNONYM_MAP = {
    "打开": "开", "关闭": "关", "关掉": "关",
    "帮我": "", "请": "", "一下": "", "好吗": "", "可以吗": "",
    "我想": "", "我要": "", "能不能": "", "可不可以": "",
}


def normalize(text: str) -> str:
    """标准化指令文本：去语气词、标点、同义词，用于缓存匹配"""
    result = text.strip()
    result = FILLER_WORDS.sub('', result)
    result = PUNCTUATION.sub('', result)
    for full, short in SYNONYM_MAP.items():
        result = result.replace(full, short)
    return result


def _make_key(text: str) -> str:
    """生成缓存键（标准化文本的 MD5 哈希）"""
    norm = normalize(text)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


# ===== SQLite 持久层 =====
DB_PATH = None  # 在 init 时设置


def _get_db_path() -> str:
    import os
    if DB_PATH:
        return DB_PATH
    # 默认存放在 backend 目录
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache.db")


def _init_db():
    """初始化 SQLite 表"""
    conn = sqlite3.connect(_get_db_path())
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            id TEXT PRIMARY KEY,
            normalized_text TEXT NOT NULL,
            original_text TEXT NOT NULL,
            reply TEXT DEFAULT '',
            hit_count INTEGER DEFAULT 0,
            llm_call_count INTEGER DEFAULT 0,
            is_cached INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            last_hit_at REAL NOT NULL
        )
    """)
    # 兼容旧表：如果缺少 llm_call_count 列则添加
    try:
        conn.execute("ALTER TABLE cache ADD COLUMN llm_call_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cache ADD COLUMN is_cached INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE cache ADD COLUMN actions_json TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


# ===== 缓存引擎 =====
class CacheEngine:
    """内存热层（LRU） + SQLite 冷层 双层缓存"""

    def __init__(self, hot_size: int = 200):
        _init_db()
        self.hot: OrderedDict[str, dict] = OrderedDict()
        self.hot_size = hot_size
        # 从 SQLite 加载已有条目
        self._load_from_db()

    def _load_from_db(self):
        """启动时从 SQLite 加载缓存到内存热层"""
        conn = sqlite3.connect(_get_db_path())
        # 尝试加载 actions_json 列（兼容旧表无此列）
        try:
            rows = conn.execute(
                "SELECT id, normalized_text, original_text, reply, hit_count, created_at, last_hit_at, actions_json "
                "FROM cache ORDER BY last_hit_at DESC LIMIT ?",
                (self.hot_size,)
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT id, normalized_text, original_text, reply, hit_count, created_at, last_hit_at "
                "FROM cache ORDER BY last_hit_at DESC LIMIT ?",
                (self.hot_size,)
            ).fetchall()
        conn.close()
        for row in rows:
            entry = {
                "id": row[0], "normalized_text": row[1], "original_text": row[2],
                "reply": row[3], "hit_count": row[4],
                "created_at": row[5], "last_hit_at": row[6],
            }
            if len(row) >= 8:
                entry["actions_json"] = row[7]
            self.hot[row[0]] = entry

    CACHE_THRESHOLD = 3  # 同一指令第 3 次调用后缓存

    def check_and_get(self, text: str) -> dict | None:
        """
        查询缓存。仅返回已缓存（is_cached=1）且未过期的条目。
        命中时更新热层和命中计数。
        """
        key = _make_key(text)
        now = time.time()

        # 先查热层
        if key in self.hot and self.hot[key].get("is_cached"):
            entry = self.hot[key]
            if now - entry["last_hit_at"] > 7 * 24 * 3600:
                self.delete(text)
                return None
            self.hot.move_to_end(key)
            entry["hit_count"] += 1
            entry["last_hit_at"] = now
            self._update_db_hit(key, entry["hit_count"], now)
            return entry

        # 查冷层
        conn = sqlite3.connect(_get_db_path())
        try:
            row = conn.execute(
                "SELECT id, reply, hit_count, last_hit_at, actions_json FROM cache WHERE id = ? AND is_cached = 1", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = conn.execute(
                "SELECT id, reply, hit_count, last_hit_at FROM cache WHERE id = ? AND is_cached = 1", (key,)
            ).fetchone()
        conn.close()
        if row:
            entry = {"id": row[0], "reply": row[1], "hit_count": row[2], "last_hit_at": row[3], "is_cached": True}
            if len(row) >= 5 and row[4]:
                entry["actions_json"] = row[4]
            if now - entry["last_hit_at"] > 7 * 24 * 3600:
                self.delete(text)
                return None
            entry["hit_count"] += 1
            self._promote_to_hot(key, entry)
            self._update_db_hit(key, entry["hit_count"], now)
            return entry
        return None

    def increment_and_check(self, text: str) -> tuple[int, bool]:
        """
        大模型路径调用计数 +1。
        Returns: (当前次数, 是否达到缓存阈值)
        """
        key = _make_key(text)
        now = time.time()
        conn = sqlite3.connect(_get_db_path())
        row = conn.execute("SELECT llm_call_count FROM cache WHERE id = ?", (key,)).fetchone()
        current = (row[0] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO cache (id, normalized_text, original_text, reply, hit_count, "
            "llm_call_count, is_cached, created_at, last_hit_at) "
            "VALUES (?, ?, ?, '', 0, ?, 0, ?, ?)",
            (key, normalize(text), text, current, now, now)
        )
        conn.commit()
        conn.close()
        return current, current >= self.CACHE_THRESHOLD

    def store_reply(self, text: str, reply: str, actions_json: str | None = None):
        """缓存大模型回复（达到阈值时调用）

        Args:
            actions_json: 若提供，为 [ACTIONS] 标签中解析出的 JSON 字符串，
                         缓存命中时将恢复执行（音乐/设备操作）。
        """
        key = _make_key(text)
        now = time.time()
        entry = {"id": key, "reply": reply, "hit_count": 0, "last_hit_at": now, "is_cached": True}
        if actions_json:
            entry["actions_json"] = actions_json
        self._promote_to_hot(key, entry)
        conn = sqlite3.connect(_get_db_path())
        conn.execute(
            "UPDATE cache SET reply = ?, actions_json = ?, is_cached = 1, hit_count = 0, last_hit_at = ? WHERE id = ?",
            (reply, actions_json, now, key)
        )
        conn.commit()
        conn.close()

    def delete(self, text_or_id: str):
        """删除缓存条目"""
        # 尝试作为 ID 或文本处理
        key = text_or_id if len(text_or_id) == 32 else _make_key(text_or_id)
        self.hot.pop(key, None)
        conn = sqlite3.connect(_get_db_path())
        conn.execute("DELETE FROM cache WHERE id = ?", (key,))
        conn.commit()
        conn.close()

    def get_all(self) -> list[dict]:
        """获取全部缓存条目（用于前端管理面板）"""
        conn = sqlite3.connect(_get_db_path())
        try:
            rows = conn.execute(
                "SELECT id, normalized_text, original_text, reply, hit_count, created_at, last_hit_at, actions_json "
                "FROM cache ORDER BY last_hit_at DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT id, normalized_text, original_text, reply, hit_count, created_at, last_hit_at "
                "FROM cache ORDER BY last_hit_at DESC"
            ).fetchall()
        conn.close()
        results = []
        for r in rows:
            entry = {
                "id": r[0], "normalized_text": r[1], "original_text": r[2],
                "reply": r[3], "hit_count": r[4],
                "created_at": r[5], "last_hit_at": r[6],
            }
            if len(r) >= 8 and r[7]:
                entry["actions_json"] = r[7]
            results.append(entry)
        return results

    def _promote_to_hot(self, key: str, entry: dict):
        """提升条目到热层"""
        if key in self.hot:
            self.hot.move_to_end(key)
        else:
            self.hot[key] = entry
            if len(self.hot) > self.hot_size:
                self.hot.popitem(last=False)

    def _update_db_hit(self, key: str, hit_count: int, last_hit_at: float):
        """同步命中统计到 SQLite"""
        conn = sqlite3.connect(_get_db_path())
        conn.execute(
            "UPDATE cache SET hit_count = ?, last_hit_at = ? WHERE id = ?",
            (hit_count, last_hit_at, key)
        )
        conn.commit()
        conn.close()

    def cleanup_expired(self) -> int:
        """清理过期条目（超过 7 天未命中），返回清理数"""
        cutoff = time.time() - 7 * 24 * 3600
        # SQLite
        conn = sqlite3.connect(_get_db_path())
        cursor = conn.execute("DELETE FROM cache WHERE last_hit_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        # 热层
        expired_keys = [k for k, v in self.hot.items() if v["last_hit_at"] < cutoff]
        for k in expired_keys:
            del self.hot[k]
        return deleted + len(expired_keys)


# 全局单例
_cache_engine: CacheEngine | None = None


def get_cache() -> CacheEngine:
    global _cache_engine
    if _cache_engine is None:
        _cache_engine = CacheEngine()
    return _cache_engine
