"""
双引擎调度配置 —— 可持久化的调度规则
"""
import json
import os
import logging

logger = logging.getLogger(__name__)

# 配置文件路径（项目根目录 .reasonix/engine_config.json）
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", ".reasonix", "engine_config.json",
)

DEFAULT_CONFIG = {
    # 默认调度模式
    # local_first: 本地优先，超时或特定任务切云端
    # cloud_first: 云端优先，离线切本地（用户有 API Key 时推荐）
    # local_only:  强制本地
    # cloud_only:  强制云端
    "default_mode": "local_first",

    # 本地模型超时（秒），超时后自动切换到云端
    "timeout_seconds": 8,

    # 强制使用云端的触发词（含这些词的任务走云端）
    "force_cloud_keywords": [
        "搜索", "查询", "实时", "今天", "最新",
        "股票", "汇率", "新闻", "头条", "热搜",
    ],

    # 强制使用本地的触发词（含这些词的任务必须本地，如隐私）
    "force_local_keywords": [
        "我的密码", "家里的", "隐私", "私密", "保密",
        "我的地址", "我的电话", "我的身份证",
    ],

    # 云端模型（仅支持 deepseek 实际存在的模型名）
    "cloud_model": "deepseek-v4-flash",

    # 本地模型
    "local_model": "qwen2.5:7b",

    # 联网搜索
    "enable_search": True,
    "search_provider": "duckduckgo",  # duckduckgo | searxng
    "searxng_url": "",

    # 生成长度阈值（超过此长度的文本强制走云端，0=不启用）
    "long_text_threshold": 0,
}


class EngineConfig:
    """双引擎调度配置管理器"""

    def __init__(self):
        self._config = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    loaded = json.load(f)
                    # 只更新存在的键
                    for k in loaded:
                        if k in self._config:
                            self._config[k] = loaded[k]
                    logger.info(f"引擎配置已加载: {_CONFIG_PATH}")
        except Exception as e:
            logger.warning(f"加载引擎配置失败: {e}")

    def save(self):
        """持久化配置到文件"""
        try:
            os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            logger.info(f"引擎配置已保存: {_CONFIG_PATH}")
            return True
        except Exception as e:
            logger.error(f"保存引擎配置失败: {e}")
            return False

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def set(self, key: str, value):
        if key in self._config:
            self._config[key] = value
            self.save()

    def to_dict(self) -> dict:
        return dict(self._config)

    def resolve_mode(self, text: str) -> str:
        """
        根据输入文本和配置，决定实际使用的调度模式。
        Returns: 'local' | 'cloud'
        """
        mode = self._config["default_mode"]
        if mode == "local_only":
            return "local"
        if mode == "cloud_only":
            return "cloud"

        # 检查强制本地关键词
        for kw in self._config["force_local_keywords"]:
            if kw in text:
                logger.info(f"隐私关键词匹配「{kw}」→ 强制本地")
                return "local"

        if mode == "cloud_first":
            return "cloud"

        # local_first: 检查强制云端关键词
        if mode == "local_first":
            for kw in self._config["force_cloud_keywords"]:
                if kw in text:
                    logger.info(f"实时关键词匹配「{kw}」→ 强制云端")
                    return "cloud"
            return "local"

        return "local"


# 全局单例
_config: EngineConfig | None = None


def get_config() -> EngineConfig:
    global _config
    if _config is None:
        _config = EngineConfig()
    return _config
