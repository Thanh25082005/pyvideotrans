# -*- coding: utf-8 -*-
"""Tests that the translator/__init__.py split preserves all public imports and behaviour."""


class TestTranslatorSplitImports:
    """Verify every commonly-imported name is reachable from videotrans.translator."""

    def test_run_importable(self):
        from videotrans.translator import run
        assert callable(run)

    def test_get_code_importable(self):
        from videotrans.translator import get_code
        assert callable(get_code)

    def test_get_source_target_code_importable(self):
        from videotrans.translator import get_source_target_code
        assert callable(get_source_target_code)

    def test_get_language_qwen_importable(self):
        from videotrans.translator import get_language_qwen
        assert callable(get_language_qwen)

    def test_is_allow_translate_importable(self):
        from videotrans.translator import is_allow_translate
        assert callable(is_allow_translate)

    def test_get_audio_code_importable(self):
        from videotrans.translator import get_audio_code
        assert callable(get_audio_code)

    def test_get_subtitle_code_importable(self):
        from videotrans.translator import get_subtitle_code
        assert callable(get_subtitle_code)

    def test_get_mkv_code_importable(self):
        from videotrans.translator import get_mkv_code
        assert callable(get_mkv_code)

    def test_lang_code_importable(self):
        from videotrans.translator import LANG_CODE
        assert isinstance(LANG_CODE, dict)
        assert 'en' in LANG_CODE
        assert 'zh-cn' in LANG_CODE

    def test_langname_dict_importable(self):
        from videotrans.translator import LANGNAME_DICT
        assert isinstance(LANGNAME_DICT, dict)

    def test_langname_dict_rev_importable(self):
        from videotrans.translator import LANGNAME_DICT_REV
        assert isinstance(LANGNAME_DICT_REV, dict)

    def test_id_name_dict_importable(self):
        from videotrans.translator import _ID_NAME_DICT
        assert isinstance(_ID_NAME_DICT, dict)
        assert len(_ID_NAME_DICT) == 2

    def test_translaste_name_list_importable(self):
        from videotrans.translator import TRANSLASTE_NAME_LIST
        assert isinstance(TRANSLASTE_NAME_LIST, list)
        assert len(TRANSLASTE_NAME_LIST) == 2

    def test_ai_trans_channels_importable(self):
        from videotrans.translator import AI_TRANS_CHANNELS
        assert isinstance(AI_TRANS_CHANNELS, list)
        assert len(AI_TRANS_CHANNELS) == 14

    def test_base_trans_importable(self):
        from videotrans.translator import BaseTrans
        assert BaseTrans is not None


class TestTranslatorIndexConstants:
    """Chỉ còn hai kênh dịch: ChatGPT và Custom API."""

    def test_chatgpt_index(self):
        from videotrans.translator import CHATGPT_INDEX
        assert CHATGPT_INDEX == 0

    def test_transapi_index(self):
        from videotrans.translator import TRANSAPI_INDEX
        assert TRANSAPI_INDEX == 1

    def test_ai_channels_contains_chatgpt(self):
        from videotrans.translator import AI_TRANS_CHANNELS, CHATGPT_INDEX
        assert CHATGPT_INDEX in AI_TRANS_CHANNELS

    def test_index_matches_list_position(self):
        """Giao diện dùng vị trí trong danh sách làm ID nên hai thứ phải khớp."""
        from videotrans.translator import _ID_NAME_DICT
        assert list(_ID_NAME_DICT.keys()) == list(range(len(_ID_NAME_DICT)))


class TestTranslatorGetCode:
    """Verify get_code() function works correctly."""

    def test_none_returns_none(self):
        from videotrans.translator import get_code
        assert get_code(None) is None

    def test_dash_returns_none(self):
        from videotrans.translator import get_code
        assert get_code('-') is None

    def test_no_returns_none(self):
        from videotrans.translator import get_code
        assert get_code('No') is None

    def test_empty_string_returns_none(self):
        from videotrans.translator import get_code
        assert get_code('') is None

    def test_zh_maps_to_zh_cn(self):
        from videotrans.translator import get_code
        assert get_code('zh') == 'zh-cn'

    def test_lang_code_passthrough(self):
        from videotrans.translator import get_code
        assert get_code('en') == 'en'
        assert get_code('fr') == 'fr'
        assert get_code('ja') == 'ja'

    def test_display_name_returns_code(self):
        from videotrans.translator import get_code
        from videotrans.translator import LANGNAME_DICT_REV
        for display_name, code in list(LANGNAME_DICT_REV.items())[:5]:
            result = get_code(display_name)
            assert result == code, f"get_code({display_name!r}) = {result!r}, expected {code!r}"


class TestTranslatorGetSourceTargetCode:
    def test_ai_channel(self):
        """Kênh LLM nhận tên ngôn ngữ dạng tự nhiên để đưa vào prompt."""
        from videotrans.translator import get_source_target_code, CHATGPT_INDEX
        src, tgt = get_source_target_code(
            show_source='en', show_target='zh-cn', translate_type=CHATGPT_INDEX
        )
        assert src == 'English'
        assert tgt == 'Simplified Chinese'

    def test_custom_api_channel(self):
        """Kênh API tuỳ biến nhận mã ngôn ngữ."""
        from videotrans.translator import get_source_target_code, TRANSAPI_INDEX
        src, tgt = get_source_target_code(
            show_source='en', show_target='zh-cn', translate_type=TRANSAPI_INDEX
        )
        assert src == 'en'
        assert tgt == 'zh-cn'

    def test_dash_source_skipped(self):
        from videotrans.translator import get_source_target_code, TRANSAPI_INDEX
        src, tgt = get_source_target_code(
            show_source='-', show_target='zh-cn', translate_type=TRANSAPI_INDEX
        )
        assert src == '-'
        assert tgt == 'zh-cn'


class TestTranslatorIsAllowTranslate:
    def test_none_channel_allowed(self):
        from videotrans.translator import is_allow_translate
        assert is_allow_translate(translate_type=None) is True

    def test_unknown_channel_allowed(self):
        from videotrans.translator import is_allow_translate
        assert is_allow_translate(translate_type=999) is True


class TestTranslatorAudioCode:
    """Verify get_audio_code() function works correctly."""

    def test_auto_for_none(self):
        from videotrans.translator import get_audio_code
        assert get_audio_code(show_source=None) == 'auto'

    def test_auto_for_dash(self):
        from videotrans.translator import get_audio_code
        assert get_audio_code(show_source='-') == 'auto'

    def test_auto_for_auto(self):
        from videotrans.translator import get_audio_code
        assert get_audio_code(show_source='auto') == 'auto'

    def test_english_code(self):
        from videotrans.translator import get_audio_code
        assert get_audio_code(show_source='en') == 'en'

    def test_zh_cn_code(self):
        from videotrans.translator import get_audio_code
        assert get_audio_code(show_source='zh-cn') == 'zh-cn'


class TestTranslatorSubtitleCode:
    """Verify get_subtitle_code() function works correctly."""

    def test_english(self):
        from videotrans.translator import get_subtitle_code
        assert get_subtitle_code(show_target='en') == 'eng'

    def test_zh_cn(self):
        from videotrans.translator import get_subtitle_code
        assert get_subtitle_code(show_target='zh-cn') == 'zho'

    def test_fallback_to_eng(self):
        from videotrans.translator import get_subtitle_code
        assert get_subtitle_code(show_target='nonexistent') == 'eng'


class TestTranslatorMkvCode:
    """Verify get_mkv_code() function works correctly."""

    def test_fra_to_fre(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('fra') == 'fre'

    def test_deu_to_ger(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('deu') == 'ger'

    def test_zho_to_chi(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('zho') == 'chi'

    def test_ces_to_cze(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('ces') == 'cze'

    def test_ell_to_gre(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('ell') == 'gre'

    def test_fas_to_per(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('fas') == 'per'

    def test_msa_to_may(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('msa') == 'may'

    def test_nld_to_dut(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('nld') == 'dut'

    def test_ron_to_rum(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('ron') == 'rum'

    def test_unknown_code_passthrough(self):
        from videotrans.translator import get_mkv_code
        assert get_mkv_code('eng') == 'eng'
        assert get_mkv_code('jpn') == 'jpn'


class TestTranslatorRunCallable:
    """Verify run() is callable and has correct signature."""

    def test_run_is_callable(self):
        from videotrans.translator import run
        import inspect
        assert callable(run)
        sig = inspect.signature(run)
        params = list(sig.parameters.keys())
        assert 'translate_type' in params
        assert 'text_list' in params
        assert 'is_test' in params
        assert 'source_code' in params
        assert 'target_code' in params
        assert 'uuid' in params
