"""
用户记忆引擎 —— 从对话中提取用户信息，在后续对话中引用
"""
import asyncio
import json
import logging
import re
import sqlite3
import os
import time
from collections import OrderedDict

import ollama

logger = logging.getLogger(__name__)


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory.db")

# 记忆提取模式：识别用户自我陈述的关键句式
EXTRACTION_PATTERNS = [
    # 姓名
    (r'我(?:叫|的名字是)([^，。,\.\s]{2,8})', 'name', '用户名叫{value}'),
    # 日程
    (r'我(?:每天|早上|通常)(\d{1,2}[:：点]\d{0,2})?(?:.*?)(出门|上班|上学|起床|睡觉)', 'schedule', '用户{value}'),
    (r'我(?:每天|通常|一般)(\d{1,2}[:：点]\d{0,2})', 'schedule', '用户{value}'),
    # 偏好
    (r'我(?:喜欢|特别喜欢|非常喜欢|最爱|爱|喜欢听|喜欢看|喜欢吃|不喜欢|讨厌)(.{2,20}?)(?:[，。,!\.]|$)', 'preference', '用户喜欢{value}'),
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
    (r'我(?:最近在学|在学|正在学习|正在备考|备考|准备考|在准备|学习)(.{2,20}?)(?:[，。,!\.]|$)', 'learning', '用户在学习{value}'),
    # 家庭
    (r'我(?:妈妈|爸爸|老婆|老公|孩子|女儿|儿子|女朋友|男朋友|对象)(?:叫|是|今年)(.{2,15}?)(?:[，。,!\.]|$)', 'family', '用户家人{value}'),
    # 健康（需含健康关键词，避免"我最近在学英语"误匹配）
    (r'我最近(?:睡眠|身体|胃口|精神|状态|睡|困|累|疼|痛|不舒服|不太舒服|有点难受|感冒|发烧|生病)(.{1,15}?)(?:[，。,!\.]|$)', 'health', '用户健康{value}'),
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

【重要】只提取关于用户的长期偏好、身份、习惯、日程信息。
不要提取一次性任务请求或临时指令。
如果用户说"我想做真题""我要听歌""帮我查一下天气"，这些是临时请求，不是偏好，不要记录。
只有当用户明确表达长期喜好（如"我喜欢周杰伦""我每天早上7点起床"）时才记录。

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
        "被烤", "背考", "贝考", "备烤", "背靠", "被靠", "贝靠",
        "六集", "六极", "留级", "六业", "六页",
        "真体", "魔女席", "整体券",
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
                if any(noise in value for noise in self.STT_NOISE_WORDS):
                    continue
                # 语义校验
                if not self._is_semantically_valid(category, value):
                    continue
                if self._is_noise_memory(value):
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

    def _is_semantically_valid(self, category: str, value: str) -> bool:
        """语义校验：记忆值必须有实际意义，不能是请求/命令/噪声"""
        if not value or len(value) < 2 or len(value) > 30:
            return False

        # 噪声词过滤（STT 常见误识别碎片）
        value_lower = value.lower().strip()
        noise_fragments = {
            "呃呃", "嗯嗯", "那个", "就是", "然后", "所以",
            "可以", "好的", "好吧", "对", "是", "不是",
            "有", "没有", "会", "不会", "能", "不能",
            "um", "uh", "er", "ah", "oh",
            "啊", "吧", "呢", "吗", "哦", "嗯",
        }
        if value_lower in noise_fragments:
            return False

        # 值不应该是完整的请求/命令句式
        request_patterns = [
            r'我想', r'我要', r'帮我', r'请', r'能不能', r'可以不可以',
            r'告诉我', r'查一下', r'放', r'播放', r'搜', r'搜索',
            r'打开', r'关闭', r'切换', r'调到', r'设置',
        ]
        for pat in request_patterns:
            if pat in value:
                return False

        # 类别一致性校验
        if category == 'name':
            # 名字应该是 2-4 个中文字符或 2-15 个英文字母
            has_chinese = any('一' <= c <= '鿿' for c in value)
            if has_chinese and (len(value) < 2 or len(value) > 5):
                return False
            if not has_chinese and (len(value) < 2 or len(value) > 20):
                return False
            # 名字不应该包含标点符号
            if re.search(r'[，。！？、""''（）《》{}]', value):
                return False

        if category == 'age':
            # 年龄应该是数字
            if not re.match(r'^\d{1,3}(?:岁|周岁)?$', value):
                return False

        if category == 'schedule':
            # 日程应该有时间信息或动作词
            has_time = bool(re.search(r'\d{1,2}[：:点]', value))
            has_action = any(w in value for w in ['出门', '上班', '上学', '起床', '睡觉', '锻炼', '跑步'])
            if not has_time and not has_action:
                return False

        return True

    def _is_noise_memory(self, value: str) -> bool:
        """检测记忆值是否为 STT 噪声/低质量文本"""
        # 纯标点/空白
        if not re.search(r'[一-鿿\w]', value):
            return True
        # 纯数字（少于3位且无上下文）
        if re.match(r'^\d{1,2}$', value):
            return True
        # 重复字符模式（如 "啊啊啊"）
        if len(value) >= 3 and len(set(value)) <= 2:
            return True
        return False

    async def extract_with_llm(self, user_text: str, assistant_text: str) -> list[dict]:
        """使用 LLM 从对话中提取用户记忆（异步，不阻塞主回复）。

        直调 ollama.chat（绕过双引擎调度器），使用低温度减少幻觉。
        失败时静默返回空列表，不影响主流程。
        """
        if not user_text or not user_text.strip():
            return []

        try:
            prompt = MEMORY_EXTRACTION_PROMPT.format(
                user_text=user_text[:500],
                assistant_text=assistant_text[:500],
            )
            messages = [{"role": "user", "content": prompt}]

            # 在线程池中运行 ollama.chat（避免阻塞事件循环）
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ollama.chat(
                    model="qwen3:8b",
                    messages=messages,
                    stream=False,
                    options={"temperature": 0.3, "top_p": 0.9},
                )
            )
            full_reply = (response.get("message") or {}).get("content", "")

            if not full_reply.strip():
                return []

            # 解析 JSON: LLM 可能输出纯 JSON 或包裹在 markdown 代码块中
            json_match = re.search(r'\[[\s\S]*?\]', full_reply)
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
                if any(noise in value for noise in self.STT_NOISE_WORDS):
                    continue
                if not self._is_semantically_valid(category, value):
                    continue
                if self._is_noise_memory(value):
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
                logger.info(f"LLM 记忆提取: {stored}")
            return stored

        except Exception:
            logger.debug("LLM 记忆提取失败（非关键错误）")
            return []

    def get_context(self) -> str:
        """获取所有记忆，格式化后注入 LLM 系统提示词。
        过滤噪声记忆，按类别去重（同一类别只保留最新一条）。"""
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT category, value_text FROM memories ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
        if not rows:
            return ""

        # 过滤：去重 + 排除噪声
        seen_categories: set[str] = set()
        seen_values: set[str] = set()
        filtered: list[tuple[str, str]] = []
        for cat, val in rows:
            if self._is_noise_memory(val):
                continue
            # 同类别只保留最新一条（避免上下文膨胀）
            if cat in seen_categories:
                continue
            # 完全相同的值去重
            val_key = val.strip().lower()
            if val_key in seen_values:
                continue
            seen_categories.add(cat)
            seen_values.add(val_key)
            filtered.append((cat, val))

        lines = ["\n关于用户，你已知的信息："]
        for cat, val in filtered:
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
