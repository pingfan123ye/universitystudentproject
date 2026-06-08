"""测试 STT 纠错器：精确替换、幻觉检测、低质量过滤"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.stt_corrector import correct, _is_hallucination, is_low_quality_stt, CORRECTIONS


class TestCorrect:
    """精确替换纠错"""

    def test_wake_word_correction(self):
        assert correct("小字你好") == "小智你好"
        assert correct("小子帮我开灯") == "小智帮我开灯"
        assert correct("小志播放音乐") == "小智播放音乐"

    def test_music_command_correction(self):
        assert correct("切割下一首") == "切歌下一首"
        assert correct("切割") == "切歌"
        assert correct("暂停拨号") == "暂停播放"

    def test_cet6_correction(self):
        # SenseVoice 准确率提升后，精简版 CORRECTIONS 仅保留音近高频纠错
        assert correct("被烤六集") == "备考六级"  # 被烤→备考 + 六集→六级
        assert correct("备烤六极") == "备考六级"  # 备烤→备考 + 六极→六级
        # "背靠"、"真体券" 等已从纠错表移除（SenseVoice 可正确识别）

    def test_learning_correction(self):
        # "学系" → "学习" 已移除（SenseVoice 可区分"系"和"习"）
        # 空文本/纯空格保持不变
        assert correct("") == ""

    def test_empty_input(self):
        assert correct("") == ""
        assert correct("   ") == "   "  # correct() 保持空白不变


class TestIsHallucination:
    """幻觉检测"""

    def test_subtitle_hallucination(self):
        assert _is_hallucination("字幕by索兰娅") is True
        assert _is_hallucination("字幕 by 索兰娅") is True

    def test_english_long_text(self):
        assert _is_hallucination("hello world this is a long english sentence") is True

    def test_repeated_chars(self):
        assert _is_hallucination("呃呃呃呃") is True

    def test_normal_text(self):
        assert _is_hallucination("帮我开灯") is False
        assert _is_hallucination("播放晴天") is False
        # 注意："谢谢" 是 2 字符 2 唯一字，不满足 ≥3 字符且 ≤2 唯一字条件
        # 它在音乐串扰检测 intent_router._is_music_bleed 中被处理


class TestIsLowQualitySTT:
    """低质量 STT 文本过滤"""

    def test_empty(self):
        assert is_low_quality_stt("") is True
        assert is_low_quality_stt("  ") is True

    def test_too_short(self):
        assert is_low_quality_stt("ab") is True

    def test_repeated_chars(self):
        assert is_low_quality_stt("嗯嗯嗯") is True
        assert is_low_quality_stt("好好好") is True

    def test_english_gibberish(self):
        assert is_low_quality_stt("hello world this is a test sentence with many words") is True

    def test_normal_chinese(self):
        assert is_low_quality_stt("帮我打开灯") is False
        assert is_low_quality_stt("播放周杰伦的歌") is False
        assert is_low_quality_stt("你好小智") is False


class TestCorrectionsCoverage:
    """验证精简版 CORRECTIONS 字典的覆盖范围（SenseVoice 准确率提升后大幅精简）"""

    def test_has_cet6_corrections(self):
        """确保 CET-6 核心音近纠错规则存在"""
        cet6_pairs = [
            ("背考", "备考"), ("被烤", "备考"), ("备烤", "备考"),
            ("六集", "六级"), ("六极", "六级"),
        ]
        for wrong, right in cet6_pairs:
            assert CORRECTIONS.get(wrong) == right, f"Missing: {wrong} → {right}"

    def test_has_music_corrections(self):
        """确保音乐指令核心音近纠错规则存在"""
        music_pairs = [
            ("切割", "切歌"), ("切格", "切歌"),
            ("暂停拨号", "暂停播放"), ("展厅播放", "暂停播放"),
            ("波放", "播放"), ("播发", "播放"),
        ]
        for wrong, right in music_pairs:
            assert CORRECTIONS.get(wrong) == right, f"Missing: {wrong} → {right}"
