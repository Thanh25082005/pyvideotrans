from videotrans.configure.config import params, logger
from videotrans import winform
from videotrans.translator._constants import TRANSAPI_INDEX, AI_TRANS_CHANNELS
from videotrans.translator._lang_codes import LANGNAME_DICT_REV, LANG_CODE
from videotrans.translator._registry import _ID_NAME_DICT

# 根据语言显示文字，返回语言代码，show_text 可能是显示文字或本身已是语言代码
def get_code(show_text=None):
    # - None 即不选择语言，则返回 None，调用处需根据返回结果判断
    # 未在 LANG CODE 中找到则原样返回
    if not show_text or show_text in ['-', 'No']:
        return None
    if show_text == 'zh':
        return 'zh-cn'
    # 是语言代码本身，例如 zh-cn,en
    if show_text in LANG_CODE:
        return show_text
    # 是语言显示名称，例如 简体中文，English
    return LANGNAME_DICT_REV.get(show_text, show_text)


# 根据显示的语言和翻译通道，获取该翻译通道要求的源语言代码和目标语言代码
# translate_type 翻译通道索引
# show_source 显示的原语言名称或 - 或  语言代码
# show_target 显示的目标语言名称 或 - 或语言代码
# 如果是AI渠道则返回语言的自然语言名称
# 新增的语言代码直接返回
# - No 是兼容早期不规范写法
def get_source_target_code(*, show_source=None, show_target=None, translate_type=None):
    source_list = None
    target_list = None

    if show_source and show_source not in ['-', 'No']:
        if show_source in LANG_CODE:  # 是语言代码
            source_list = LANG_CODE[show_source]
        elif LANGNAME_DICT_REV.get(show_source):  # 是语言显示名字
            source_list = LANG_CODE.get(LANGNAME_DICT_REV.get(show_source))
        elif show_source == 'zh':  # 特殊兼容zh
            source_list = LANG_CODE['zh-cn']

    if show_target and show_target not in ['-', 'No']:
        if show_target in LANG_CODE:  # 是语言代码
            target_list = LANG_CODE[show_target]
        elif LANGNAME_DICT_REV.get(show_target):  # 语言名字
            target_list = LANG_CODE.get(LANGNAME_DICT_REV.get(show_target))
        elif show_target == 'zh':
            # 特殊兼容zh
            target_list = LANG_CODE['zh-cn']

    # 均未找到，可能是新增语言代码
    if not source_list and not target_list:
        return show_source, show_target  # 返回原始输入

    # Kênh LLM đang mang ID 0 nên phải so sánh với None, `not translate_type` sẽ nuốt mất nó
    # Kênh API tuỳ biến (và khi chưa chọn kênh): trả về mã ngôn ngữ
    if translate_type is None or translate_type == TRANSAPI_INDEX:
        return source_list[0] if source_list else show_source, target_list[0] if target_list else show_target

    # Kênh LLM: trả về tên ngôn ngữ dạng tự nhiên để đưa vào prompt
    if translate_type in AI_TRANS_CHANNELS:
        return source_list[7] if source_list else show_source, target_list[7] if target_list else show_target

    return show_source, show_target


# 判断当前翻译通道和目标语言是否允许翻译
# 比如deepl不允许翻译到某些目标语言，某些通道是否填写api key 等
# translate_type翻译通道
# show_target 翻译后显示的目标语言名称
# only_key=True 仅检测 key 和api，不判断目标语言
def is_allow_translate(*, translate_type=None, show_target=None, only_key=False, return_str=False):
    if translate_type is None:
        return True

    _cls = _ID_NAME_DICT.get(translate_type)
    if not _cls:
        return True
    if _cls.key_name and not params.get(_cls.key_name):
        return "Please configure the SK or API information of the channel first." if return_str else winform.get_win(_cls.win).openwin()

    # Hai kênh còn lại đều dịch được mọi ngôn ngữ trong danh sách nên không chặn theo ngôn ngữ đích
    return True


# 获取用于进行语音识别的预设语言，比如语音是英文发音、中文发音
# 根据 原语言进行判断,基本等同于google，但只保留_之前的部分
def get_audio_code(*, show_source=None):
    if not show_source or show_source in ['auto', '-']:
        return 'auto'
    source_list = LANG_CODE[show_source] if show_source in LANG_CODE else LANG_CODE.get(
        LANGNAME_DICT_REV.get(show_source))
    return source_list[0] if source_list else "auto"


# 获取嵌入MP4视频嵌入软字幕的3位字母语言代码 ISO 639-2/T ，根据目标语言确定
# mkv视频需根据此返回的代码再调用 get_mkv_code 获取 ISO 639-2/B
def get_subtitle_code(*, show_target=None):
    try:
        if show_target in LANG_CODE:
            return LANG_CODE[show_target][1]
        if show_target in LANGNAME_DICT_REV:
            return LANG_CODE[LANGNAME_DICT_REV[show_target]][1]
    except Exception as e:
        logger.error(f'获取字幕嵌入3为语言代码错误:{e}')
    return 'eng'

# 如果是 mkv 软字幕，根据mp4所需code换算为  B 标准代码 ISO 639-2/B
def get_mkv_code(code):
    #  ISO 639-2/T :ISO 639-2/B
    langcode={
        "fra":"fre",
        "deu":"ger",
        "zho":"chi",
        "ces":"cze",
        "ell":"gre",
        "fas":"per",
        "msa":"may",
        "nld":"dut",
        "ron":"rum",
    }
    return langcode.get(code,code)
