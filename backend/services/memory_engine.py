"""
用户记忆引擎 —— 从对话中提取用户信息，在后续对话中引用
"""
import json
import re
import sqlite3
import os
import time
from collections import OrderedDict


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory.db")

# 记忆提取模式：识别用户自我陈述的关键句式
EXTRACTION_PATTERNS = [
    # 姓名
    (r'我(?:叫|是|的名字是)([^，。,\.\s]{2,8})', 'name', '用户名叫{value}'),
    # 日程
    (r'我(?:每天|早上|通常)(\d{1,2}[:：点]\d{0,2})?(?:.*?)(出门|上班|上学|起床|睡觉)', 'schedule', '用户{value}'),
    (r'我(?:每天|通常|一般)(\d{1,2}[:：点]\d{0,2})', 'schedule', '用户{value}'),
    # 偏好
    (r'我(?:喜欢|爱|想|喜欢听|喜欢看|喜欢玩|喜欢吃)(.{2,20}?)(?:[，。,!\.]|$)', 'preference', '用户喜欢{value}'),
    (r'我(?:不喜欢|讨厌|恨)(.{2,20}?)(?:[，。,!\.]|$)', 'preference', '用户不喜欢{value}'),
    # 位置
    (r'我(?:在|住在|家在|位于)(.{2,20}?)(?:[，。,!\.]|$)', 'location', '用户在{value}'),
    # 工作
    (r'我(?:是|做|干)(?:一个|一名|一位)?(.{2,15}?)(?:的)?(?:工作|职业|上班|程序员|设计师|医生|老师|学生|工程师)', 'job', '用户是{value}'),
    (r'我(?:在|于)(.{2,20}?)(?:工作|上班|任职)', 'job', '用户在{value}工作'),
    (r'我的(?:工作|职业)(?:是|为)(.{2,20}?)(?:[，。,!\.]|$)', 'job', '用户工作是{value}'),
    # 年龄
    (r'我(?:今年|已经|刚)(\d{1,3})(?:岁|周岁)', 'age', '用户{value}岁'),
    # 联系方式
    (r'我的(?:电话|手机|联系方式)(?:是|为)(.{5,20}?)(?:[，。,!\.]|$)', 'contact', '用户联系方式{value}'),
    # 宠物
    (r'我(?:有|养了|养)(?:一只|一个|一条|两只)?(.{1,10}?)(?:猫|狗|宠物|鱼|鸟|仓鼠)', 'pet', '用户养了{value}'),
    # 学习
    (r'我(?:最近在|在|正在)(?:学|学习|研究|准备)(.{2,20}?)(?:[，。,!\.]|$)', 'learning', '用户在学习{value}'),
    # 家庭
    (r'我(?:妈妈|爸爸|老婆|老公|孩子|女儿|儿子|女朋友|男朋友|对象)(?:叫|是|今年)(.{2,15}?)(?:[，。,!\.]|$)', 'family', '用户家人{value}'),
    # 健康
    (r'我(?:最近|身体|睡眠|胃口)(.{2,20}?)(?:[，。,!\.]|$)', 'health', '用户健康{value}'),
]


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key_text TEXT NOT NULL,
            value_text TEXT NOT NULL,
            source_text TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


MEMORY_EXTRACTION_PROMPT = """从以下对话中提取关于用户的关键信息。只输出 JSON 数组，每条包含 category 和 value。
如果用户没有透露任何新信息，输出空数组 []。

可提取的信息类别：name(姓名), age(年龄), job(工作), location(位置), preference(偏好/喜好),
schedule(日程/习惯), pet(宠物), learning(学习), family(家庭), health(健康), hobby(爱好)

示例输入："我叫小明，今年25岁，在北京做程序员，喜欢打篮球"
示例输出：[{{"category":"name","value":"小明"}},{{"category":"age","value":"25岁"}},{{"category":"job","value":"程序员"}},{{"category":"location","value":"北京"}},{{"category":"preference","value":"打篮球"}}]

用户说：{user_text}

助手回复：{assistant_text}"""


class MemoryEngine:
    def __init__(self):
        _init_db()

    # STT 常见误识别噪声词（不应被记忆）
    STT_NOISE_WORDS = {
        "被烤", "背考", "贝考", "备烤", "六集", "六极", "留级",
        "呃呃", "嗯嗯嗯", "对不起", "那个那个", "好的吧",
    }

    def extract_and_store(self, user_text: str):
        """从用户文本中提取并存储记忆"""
        stored = []
        for pattern, category, _ in EXTRACTION_PATTERNS:
            m = re.search(pattern, user_text)
            if m:
                try:
                    value = m.group(1) or m.group(2)
                except IndexError:
                    value = m.group(0)
                if not value or len(value) < 2 or len(value) > 30:
                    continue
                # 过滤 STT 噪声词
                if value.strip() in self.STT_NOISE_WORDS:
                    continue
                # 避免重复存储
                if self._exists(category, value):
                    continue
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO memories (category, key_text, value_text, source_text, created_at) VALUES (?, ?, ?, ?, ?)",
                    (category, pattern, value, user_text[:200], time.time())
                )
                conn.commit()
                conn.close()
                stored.append({"category": category, "value": value})
        return stored

    def _exists(self, category: str, value: str) -> bool:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT id FROM memories WHERE category = ? AND value_text = ?", (category, value)
        ).fetchone()
        conn.close()
        return row is not None

    async def extract_with_llm(self, user_text: str, assistant_text: str) -> list[dict]:
        """使用 LLM 从对话中提取用户记忆（异步，不阻塞主回复）。

        并行于正则提取运行，可识别正则无法捕获的复杂语义（如"最近睡得不太好" → health）。
        失败时静默返回空列表，不影响主流程。
        """
        if not user_text or not user_text.strip():
            return []

        try:
            prompt = MEMORY_EXTRACTION_PROMPT.format(
                user_text=user_text[:500],
                assistant_text=assistant_text[:500],
            )
            from services.llm_service import generate_stream, parse_actions
            # 使用本地 Ollama 快速模型（不阻塞主回复，便宜）
            full_reply = ""
            async for token in generate_stream(
                prompt,
                memory_context="",
                conversation_history=[],
                prefer_cloud=False,
                model_used=[],
                actions_out=None,
                cancel_event=None,
            ):
                full_reply += token

            if not full_reply.strip():
                return []

            # 解析 JSON: LLM 可能输出纯 JSON 或包裹在 markdown 代码块中
            import re as _re
            json_match = _re.search(r'\[[\s\S]*?\]', full_reply)
            if not json_match:
                return []

            items = json.loads(json_match.group(0))
            if not isinstance(items, list):
                return []

            stored = []
            for item in items:
                category = (item.get("category") or "").strip()
                value = (item.get("value") or "").strip()
                if not category or not value or len(value) < 2 or len(value) > 30:
                    continue
                if value.strip() in self.STT_NOISE_WORDS:
                    continue
                if self._exists(category, value):
                    continue
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO memories (category, key_text, value_text, source_text, created_at) VALUES (?, ?, ?, ?, ?)",
                    (category, "llm_extraction", value, user_text[:200], time.time())
                )
                conn.commit()
                conn.close()
                stored.append({"category": category, "value": value, "source": "llm"})

            if stored:
                import logging
                logger = logging.getLogger(__name__)
                logger.info(f"LLM 记忆提取: {stored}")
            return stored

        except Exception:
            import logging
            logger = logging.getLogger(__name__)
            logger.debug(f"LLM 记忆提取失败（非关键错误）")
            return []

    def get_context(self) -> str:
        """获取所有记忆，格式化后注入 LLM 系统提示词"""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT category, value_text FROM memories ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["\n关于用户，你已知的信息："]
        for cat, val in rows:
            label = {
                "name": "用户名", "schedule": "日程", "preference": "喜好",
                "location": "位置", "job": "工作", "age": "年龄",
                "contact": "联系方式", "pet": "宠物", "learning": "学习",
                "family": "家庭", "health": "健康",
            }.get(cat, cat)
            lines.append(f"- [{label}] {val}")
        return "\n".join(lines)

    def get_all(self) -> list[dict]:
        """获取全部记忆"""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, category, value_text, source_text, created_at FROM memories ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [
            {"id": r[0], "category": r[1], "value": r[2], "source": r[3], "created_at": r[4]}
            for r in rows
        ]

    def delete(self, memory_id: int):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        conn.close()

    def count(self) -> int:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        conn.close()
        return row[0] if row else 0

    def clear_all(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM memories")
        conn.commit()
        conn.close()


_memory_engine: MemoryEngine | None = None


def get_memory() -> MemoryEngine:
    global _memory_engine
    if _memory_engine is None:
        _memory_engine = MemoryEngine()
    return _memory_engine
