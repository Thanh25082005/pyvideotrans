from videotrans.configure.config import tr
from videotrans import ChannelProvider
from videotrans.translator._constants import CHATGPT_INDEX, TRANSAPI_INDEX

_ID_NAME_DICT = {
    CHATGPT_INDEX: ChannelProvider(tr('OpenAI ChatGPT'), key_name="chatgpt_key", win="chatgpt", imp="._chatgpt"),
    TRANSAPI_INDEX: ChannelProvider(tr('Customized API'), key_name="trans_api_url", win="transapi", imp="._transapi"),
}

_ID_NAME_DICT = dict(sorted(_ID_NAME_DICT.items(), key=lambda item: item[0]))
TRANSLASTE_NAME_LIST = [it.name for it in _ID_NAME_DICT.values()]
