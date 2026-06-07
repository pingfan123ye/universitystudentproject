"""测试音乐服务：查询清洗、交叉验证"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.music_service import (
    clean_music_query, _user_requested_music, _mood_to_english,
)


class TestCleanMusicQuery:
    """音乐搜索词清洗"""

    def test_simple_song(self):
        assert clean_music_query("播放晴天") == "晴天"

    def test_wake_word_removal(self):
        assert "晴天" in clean_music_query("小智小智播放晴天")
        assert "周杰伦" in clean_music_query("小智帮我播放周杰伦的稻香")

    def test_command_prefix_removal(self):
        assert clean_music_query("我想听周杰伦的歌") in ("周杰伦", "周杰伦的歌")
        assert clean_music_query("来一首晴天") == "晴天"

    def test_generic_request(self):
        assert clean_music_query("我想听歌") == ""
        assert clean_music_query("来点音乐") == ""
        assert clean_music_query("随便放一首") == ""

    def test_too_short(self):
        assert clean_music_query("a") == ""

    def test_too_long(self):
        assert clean_music_query("a" * 41) == ""

    def test_empty(self):
        assert clean_music_query("") == ""

    def test_polite_prefix(self):
        assert "晴天" in clean_music_query("好的请帮我播放晴天")

    def test_punctuation_cleanup(self):
        q = clean_music_query("播放「晴天」——周杰伦")
        assert "晴天" in q


class TestUserRequestedMusic:
    """音乐请求交叉验证"""

    def test_play_request(self):
        assert _user_requested_music("播放周杰伦的晴天") is True
        assert _user_requested_music("我想听歌") is True
        assert _user_requested_music("放首歌") is True

    def test_control_request(self):
        assert _user_requested_music("切歌") is True
        assert _user_requested_music("暂停播放") is True
        assert _user_requested_music("下一首") is True

    def test_non_music(self):
        assert _user_requested_music("今天天气怎么样") is False
        assert _user_requested_music("帮我打开灯") is False
        assert _user_requested_music("你好") is False

    def test_empty(self):
        assert _user_requested_music("") is False

    def test_ting_exclusion(self):
        # "听" 作为"听不懂"的一部分不应触发音乐
        assert _user_requested_music("我听不懂") is False
        assert _user_requested_music("听不清你说什么") is False


class TestMoodToEnglish:
    """中文情绪词 → 英文标签"""

    def test_light_music(self):
        assert _mood_to_english("轻音乐") == "instrumental piano"
        assert _mood_to_english("纯音乐") == "instrumental piano"

    def test_study(self):
        assert _mood_to_english("学习") == "study focus"
        assert _mood_to_english("做作业") == "study focus"

    def test_relax(self):
        assert _mood_to_english("放松") == "relaxing calm"
        assert _mood_to_english("安静") == "ambient study"

    def test_meditation(self):
        assert _mood_to_english("冥想") == "meditation sleep"
        assert _mood_to_english("瑜伽") == "meditation sleep"

    def test_empty(self):
        assert _mood_to_english("") == ""
        assert _mood_to_english("不匹配的关键词") == ""
