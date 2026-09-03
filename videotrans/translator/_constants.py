# ID kênh dịch. Giao diện dùng vị trí trong TRANSLASTE_NAME_LIST làm ID nên các
# giá trị này phải liên tục từ 0 và trùng thứ tự của _ID_NAME_DICT.
CHATGPT_INDEX = 0
TRANSAPI_INDEX = 1

# Kênh dịch bằng LLM (dùng prompt, dịch theo lô, có thể ngắt câu lại)
AI_TRANS_CHANNELS = [
    CHATGPT_INDEX,
]
