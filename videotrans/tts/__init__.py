from typing import Union, Type, List

from videotrans.configure.config import tr, params, app_cfg
from videotrans.tts._base import BaseTTS
from videotrans import ChannelProvider, get_class

# ID kênh lồng tiếng. Giao diện dùng vị trí trong TTS_NAME_LIST làm ID nên các
# giá trị này phải liên tục từ 0 và trùng thứ tự của _ID_NAME_DICT.
OPENAI_TTS = 0
TTS_API = 1

# Kênh hỗ trợ nhân bản giọng (role = clone)
SUPPORT_CLONE: List[int] = []
# Kênh chạy model ngay tại máy, cần xử lý riêng khi校对 lồng tiếng ở chế độ một video
LOCAL_BUILTIN: List[int] = []
# Kênh có danh sách giọng thay đổi theo ngôn ngữ
CHANGE_BY_LANGUAGE: List[int] = []

_ID_NAME_DICT = {
    OPENAI_TTS: ChannelProvider("OpenAI TTS", "._openaitts", key_name="openaitts_key", win="openaitts"),
    TTS_API: ChannelProvider(tr("Customize API"), "._ttsapi", key_name="ttsapi_url", win="ttsapi"),
}
_ID_NAME_DICT = dict(sorted(_ID_NAME_DICT.items(), key=lambda item: item[0]))
TTS_NAME_LIST = [it.name for it in _ID_NAME_DICT.values()]


# 检查当前配音渠道是否支持所选配音语言
# 返回True为支持，其他为不支持并返回错误字符串
def is_allow_lang(langcode: str = None, tts_type: int = None):
    """Hai kênh còn lại đều là API tự khai ngôn ngữ, không giới hạn tại đây."""
    return True


# 判断是否填写了相关配音渠道所需要的信息
# 正确返回True，失败返回False，并弹窗
def is_input_api(tts_type: int = None, return_str=False):
    _cls = _ID_NAME_DICT.get(tts_type)
    if not _cls:
        return True
    if _cls.key_name and not params.get(_cls.key_name):
        from videotrans import winform
        return "Please configure the SK or API information of the channel first." if return_str else winform.get_win(_cls.win).openwin()
    return True


def clone_tips(tts_type, role: str = 'No', recogn_type=0):
    if tts_type in SUPPORT_CLONE and role == 'clone':
        return tr('clone_dubb_tips1')
    return


# 统一调用 tts渠道入口，通过 tts_type 调用对应渠道
def run(*, queue_tts=None, language="", uuid=None, play=False, is_test=False, tts_type=0, is_cuda=False,is_redubb=False) -> None:
    # 需要并行的数量3
    if len(queue_tts) < 1 or app_cfg.exit_soft or (uuid and uuid in app_cfg.stoped_uuid_set): return

    kwargs = {
        "queue_tts": queue_tts,
        "language": language.lower() if language else "",
        "uuid": uuid,
        "play": play,
        "is_test": is_test,
        "tts_type": tts_type,
        "is_cuda": is_cuda,
        "is_redubb":is_redubb
    }

    _cls: Union[Type[BaseTTS], None] = get_class(tts_type, "tts", _ID_NAME_DICT)
    if not _cls:
        from videotrans.configure.excepts import DubbingSrtError
        raise DubbingSrtError(f'No this TTS Channel:{tts_type=}')

    return _cls(**kwargs).run()  # type:ignore
