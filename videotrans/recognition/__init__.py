from typing import Union, List, Type

from videotrans import winform, ChannelProvider, get_class
from videotrans.configure.config import tr, params, app_cfg, settings
from videotrans.recognition._base import BaseRecogn
from videotrans.task.taskcfg import SrtItem

# ID kênh nhận dạng. Giao diện dùng vị trí trong RECOGN_NAME_LIST làm ID nên
# các giá trị này phải liên tục từ 0 và trùng thứ tự của _ID_NAME_DICT.
OPENAI_API = 0
STT_API = 1
CUSTOM_API = 2

# Kênh cho phép chọn model khác nhau ngay trên màn hình chính
ALLOW_CHANGE_MODEL: List[int] = []

# Kênh nào cũng gọi API bên ngoài, không kèm model chạy tại máy
_ID_NAME_DICT = {
    OPENAI_API: ChannelProvider(tr("OpenAI Speech to Text"), key_name="openairecognapi_key",
                                win="openairecognapi", imp="._openairecognapi"),
    STT_API: ChannelProvider(f"STT({tr('Local')}API)", key_name="stt_url", win="sttapi", imp="._stt"),
    CUSTOM_API: ChannelProvider(tr("Custom API"), key_name="recognapi_url", win="recognapi",
                                imp="._recognapi"),
}
_ID_NAME_DICT = dict(sorted(_ID_NAME_DICT.items(), key=lambda item: item[0]))
RECOGN_NAME_LIST = [it.name for it in _ID_NAME_DICT.values()]


def get_model_by_type(recogn_type: int) -> List[str]:
    """Danh sách model cho kênh. Các kênh còn lại đều tự khai model trong cửa sổ cấu hình riêng."""
    return settings.WHISPER_MODEL_LIST


def is_allow_lang(langcode: str = None, recogn_type: int = None, model_name=None):
    """Kênh có nhận dạng được ngôn ngữ này không. True = được, ngược lại trả về chuỗi lỗi."""
    if recogn_type == OPENAI_API:
        return True
    if not langcode or langcode == 'auto':
        return tr("This channel needs an explicit source language, it cannot auto-detect.")
    return True


# Kiểm tra đã điền SK/API của kênh chưa; đúng trả True, sai thì mở cửa sổ cấu hình
def is_input_api(recogn_type: int = None, return_str=False):
    _cls = _ID_NAME_DICT.get(recogn_type)
    if not _cls: return True
    if _cls.key_name and not params.get(_cls.key_name):
        return "Please configure the API Key information of the channel first." if return_str else winform.get_win(
            _cls.win).openwin()
    return True


# 统一入口
def run(*,
        detect_language="",
        audio_file=None,
        cache_folder=None,
        model_name=None,
        uuid=None,
        recogn_type: int = 0,
        is_cuda=None,
        subtitle_type=0,
        max_speakers=-1,  # -1 不启用说话人识别,0=不限制数量，>0最大数量
        llm_post=False,
        recogn2pass=False  # 二次对配音文件识别，生成简短字幕

        ) -> Union[List[SrtItem], None]:
    if app_cfg.exit_soft or (uuid and uuid in app_cfg.stoped_uuid_set): return
    kwargs = {
        "detect_language": detect_language.lower() if detect_language else "",
        "audio_file": audio_file,
        "cache_folder": cache_folder,
        "model_name": model_name,
        "uuid": uuid,
        "is_cuda": is_cuda,
        "subtitle_type": subtitle_type,
        "recogn_type": recogn_type,
        "max_speakers": max_speakers,
        "llm_post": llm_post,
        "recogn2pass": recogn2pass
    }
    _cls: Union[Type[BaseRecogn], None] = get_class(recogn_type, "recognition", _ID_NAME_DICT)
    if not _cls:
        raise RuntimeError(f'No this Recognition Channel:{recogn_type=}')

    return _cls(**kwargs).run()  # type:ignore
