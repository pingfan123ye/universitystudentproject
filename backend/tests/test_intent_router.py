"""测试意图路由分发器"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.intent_router import (
    classify, _detect_cet6, _is_music_bleed, _check_force_llm,
    _extract_music_query,
)


class TestClassifyRoutes:
    """路由分类测试"""

    def test_device_route(self):
        d = classify("打开灯")
        assert d.path == "xiaoai"
        assert len(d.device_actions) >= 1

    def test_music_route(self):
        d = classify("播放周杰伦的歌")
        assert d.path == "xiaoai"
        assert d.music_action is not None
        assert d.music_action["action"] == "play"

    def test_music_pause(self):
        d = classify("暂停播放")
        assert d.path == "xiaoai"
        assert d.music_action is not None
        assert d.music_action["action"] == "pause"

    def test_info_query(self):
        d = classify("今天天气怎么样")
        assert d.path == "info_query"

    def test_force_llm(self):
        d = classify("你觉得今天天气怎么样")
        assert d.path == "llm"
        assert d.force_llm is True

    def test_llm_fallback(self):
        # 不含设备/音乐/信息查询关键词的普通对话走 LLM 兜底
        d = classify("你好呀小智")
        assert d.path == "llm"

    def test_empty_text(self):
        d = classify("")
        assert d.path == "llm"

    def test_noise_filter(self):
        d = classify("字幕by索兰娅")
        assert d.path == "noise"


class TestCET6Detection:
    """CET-6 检测"""

    def test_basic_cet6(self):
        assert _detect_cet6("我要备考六级") is True
        assert _detect_cet6("做六级真题") is True
        assert _detect_cet6("六级考试复习") is True

    def test_stt_corrected_input(self):
        # STT corrector 已纠正后的文本
        assert _detect_cet6("备考六级做真题") is True

    def test_music_filter(self):
        # "播放六级听力" 不应路由到 cet6
        assert _detect_cet6("播放六级听力") is False
        assert _detect_cet6("听六级") is False

    def test_non_cet6(self):
        assert _detect_cet6("播放音乐") is False
        assert _detect_cet6("今天天气怎么样") is False

    def test_short_cet6(self):
        # 短文本含"六级" → 兜底匹配
        assert _detect_cet6("六级") is True
        assert _detect_cet6("cet6") is True


class TestMusicBleed:
    """音乐串扰检测"""

    def test_subtitle_byline(self):
        assert _is_music_bleed("字幕by索兰娅") is True

    def test_pure_english(self):
        assert _is_music_bleed("hello world this is a test") is True

    def test_pure_thanks(self):
        assert _is_music_bleed("谢谢谢谢") is True
        assert _is_music_bleed("Thank you") is True

    def test_normal_command(self):
        assert _is_music_bleed("打开灯") is False
        assert _is_music_bleed("播放晴天") is False


class TestForceLLM:
    """强制走大模型检测"""

    def test_you_prefix(self):
        assert _check_force_llm("你觉得怎么样") is True
        assert _check_force_llm("你帮我看看") is True

    def test_ai_prefix(self):
        assert _check_force_llm("让AI帮我写") is True
        assert _check_force_llm("用大模型翻译") is True

    def test_normal(self):
        assert _check_force_llm("打开灯") is False
        assert _check_force_llm("播放音乐") is False


class TestExtractMusicQuery:
    """音乐查询关键词提取"""

    def test_simple_song(self):
        assert "晴天" in _extract_music_query("播放晴天")

    def test_artist_song(self):
        q = _extract_music_query("播放周杰伦的晴天")
        assert "晴天" in q or "周杰伦" in q

    def test_generic_request(self):
        assert _extract_music_query("我想听歌") == ""
        assert _extract_music_query("来点音乐") == ""

    def test_empty(self):
        assert _extract_music_query("") == ""


class TestMixedIntent:
    """混合意图检测（v0.3.0）"""

    def test_mixed_device_and_llm(self):
        d = classify("打开灯然后帮我查一下明天天气")
        assert d.path == "mixed"
        assert len(d.sub_tasks) == 2

    def test_not_mixed_without_connector(self):
        d = classify("打开灯查天气")
        # 没有连接词 → 可能走 xiaoai 或 llm，但不应走 mixed
        assert d.path != "mixed"

    def test_sensevoice_accuracy(self):
        """SenseVoice 提升准确率后，不再需要大量 STT 误识别补偿"""
        # 正确识别的音乐命令应直接路由到 xiaoai
        d = classify("播放周杰伦的晴天")
        assert d.path == "xiaoai"
        assert d.music_action is not None
        # 正确识别的设备命令
        d2 = classify("关闭卧室灯")
        assert d2.path == "xiaoai"
        assert len(d2.device_actions) >= 1
