"""Danh sách giọng đọc cho từng kênh lồng tiếng.

Chỉ còn hai kênh API: OpenAI TTS (danh sách giọng cố định) và Customize API
(giọng do người dùng tự khai trong cấu hình kênh).
"""
from typing import List

from videotrans.configure.config import params
from videotrans.configure import contants


def get_f5tts_role() -> dict:
    """Giọng tham chiếu do người dùng khai theo dạng `tên#văn bản mẫu` mỗi dòng.

    Dùng cho kênh Customize API - nơi giọng phụ thuộc hoàn toàn vào server phía sau.
    """
    rolelist = {"No": "No", "clone": "clone"}
    if not params.get('f5tts_role', '').strip():
        return rolelist
    for it in params.get('f5tts_role', '').strip().split("\n"):
        tmp = it.strip().split('#')
        if len(tmp) != 2:
            continue
        rolelist[tmp[0]] = {"ref_wav": tmp[0], "ref_text": tmp[1]}
    return rolelist


# 根据渠道返回角色列表 供下拉菜单使用
def role_menu(tts_type, langcode=None) -> List:
    from videotrans import tts

    if tts_type == tts.OPENAI_TTS:
        return ['No'] + (params.get('openaitts_role') or contants.OPENAITTS_ROLES).split(',')

    if tts_type == tts.TTS_API:
        roles = list(get_f5tts_role().keys())
        customs = params.get('ttsapi_voice_role', '').strip()
        return roles + customs.split(',') if customs else roles

    return ['No']
