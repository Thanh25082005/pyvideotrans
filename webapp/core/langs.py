"""Bảng ngôn ngữ dùng chung cho STT (Loli 2.0), dịch (OpenAI) và TTS (Loly 3.5).

Danh sách mode STT lấy từ §3 tài liệu Loli 2.0: `auto`, 30 mode đơn ngôn ngữ và
3 mode song ngữ. Ngôn ngữ đích chỉ liệt kê các mã mà cả LLM lẫn TTS đều xử lý tốt.
"""
from __future__ import annotations

# (code, tên hiển thị)
STT_LANGUAGES = [
    ("auto", "Tự động nhận diện (auto)"),
    ("vi", "Tiếng Việt"),
    ("en", "English"),
    ("zh", "中文 (Chinese)"),
    ("yue", "粵語 (Cantonese)"),
    ("ja", "日本語 (Japanese)"),
    ("ko", "한국어 (Korean)"),
    ("th", "ไทย (Thai)"),
    ("id", "Bahasa Indonesia"),
    ("ms", "Bahasa Melayu"),
    ("tl", "Filipino"),
    ("hi", "हिन्दी (Hindi)"),
    ("ar", "العربية (Arabic)"),
    ("fa", "فارسی (Persian)"),
    ("ru", "Русский (Russian)"),
    ("mk", "Македонски (Macedonian)"),
    ("el", "Ελληνικά (Greek)"),
    ("tr", "Türkçe (Turkish)"),
    ("de", "Deutsch (German)"),
    ("hu", "Magyar (Hungarian)"),
    ("da", "Dansk (Danish)"),
    ("fr", "Français (French)"),
    ("es", "Español (Spanish)"),
    ("pt", "Português (Portuguese)"),
    ("it", "Italiano (Italian)"),
    ("nl", "Nederlands (Dutch)"),
    ("pl", "Polski (Polish)"),
    ("cs", "Čeština (Czech)"),
    ("ro", "Română (Romanian)"),
    ("sv", "Svenska (Swedish)"),
    ("fi", "Suomi (Finnish)"),
    ("en-vi", "Song ngữ Anh - Việt"),
    ("ko-en", "Song ngữ Hàn - Anh"),
    ("ja-en", "Song ngữ Nhật - Anh"),
]

# code -> (tên tiếng Anh dùng cho prompt LLM, tên hiển thị trên UI)
TARGET_LANGUAGES = {
    "vi": ("Vietnamese", "Tiếng Việt"),
    "en": ("English", "English"),
    "zh": ("Simplified Chinese", "中文 (Chinese)"),
    "yue": ("Cantonese", "粵語 (Cantonese)"),
    "ja": ("Japanese", "日本語 (Japanese)"),
    "ko": ("Korean", "한국어 (Korean)"),
    "th": ("Thai", "ไทย (Thai)"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "ms": ("Malay", "Bahasa Melayu"),
    "tl": ("Filipino", "Filipino"),
    "hi": ("Hindi", "हिन्दी (Hindi)"),
    "ar": ("Arabic", "العربية (Arabic)"),
    "fa": ("Persian", "فارسی (Persian)"),
    "ru": ("Russian", "Русский (Russian)"),
    "tr": ("Turkish", "Türkçe (Turkish)"),
    "de": ("German", "Deutsch (German)"),
    "hu": ("Hungarian", "Magyar (Hungarian)"),
    "da": ("Danish", "Dansk (Danish)"),
    "fr": ("French", "Français (French)"),
    "es": ("Spanish", "Español (Spanish)"),
    "pt": ("Portuguese", "Português (Portuguese)"),
    "it": ("Italian", "Italiano (Italian)"),
    "nl": ("Dutch", "Nederlands (Dutch)"),
    "pl": ("Polish", "Polski (Polish)"),
    "cs": ("Czech", "Čeština (Czech)"),
    "ro": ("Romanian", "Română (Romanian)"),
    "sv": ("Swedish", "Svenska (Swedish)"),
    "fi": ("Finnish", "Suomi (Finnish)"),
    "el": ("Greek", "Ελληνικά (Greek)"),
    "mk": ("Macedonian", "Македонски (Macedonian)"),
}

# Ngôn ngữ viết liền không dùng khoảng trắng giữa từ -> ảnh hưởng cách gộp câu
CJK_LANGS = {"zh", "ja", "yue", "th", "km", "my", "lo"}


def english_name(code: str) -> str:
    code = (code or "").split("-")[0]
    item = TARGET_LANGUAGES.get(code)
    return item[0] if item else code


def display_name(code: str) -> str:
    item = TARGET_LANGUAGES.get(code)
    if item:
        return item[1]
    for c, label in STT_LANGUAGES:
        if c == code:
            return label
    return code


def stt_language_list():
    return [{"code": c, "label": label} for c, label in STT_LANGUAGES]


def target_language_list():
    return [{"code": c, "label": v[1]} for c, v in TARGET_LANGUAGES.items()]
