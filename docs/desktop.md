# App gốc sau khi tỉa

pyVideoTrans bản gốc có 28 kênh nhận dạng, 36 kênh lồng tiếng và 26 kênh dịch, phần lớn chạy model
ngay tại máy. Repo này chỉ giữ các kênh **gọi API bên ngoài** — đúng nguyên tắc không host model.

## Kênh còn lại

| Loại | ID | Tên | Module | Cửa sổ cấu hình |
|---|---:|---|---|---|
| Nhận dạng | 0 | OpenAI STT API | `recognition/_openairecognapi.py` | `winform/openairecognapi.py` |
| Nhận dạng | 1 | STT (Local API) | `recognition/_stt.py` | `winform/sttapi.py` |
| Nhận dạng | 2 | Custom STT API | `recognition/_recognapi.py` | `winform/recognapi.py` |
| Lồng tiếng | 0 | OpenAI TTS | `tts/_openaitts.py` | `winform/openaitts.py` |
| Lồng tiếng | 1 | Custom TTS API | `tts/_ttsapi.py` | `winform/ttsapi.py` |
| Dịch | 0 | OpenAI ChatGPT | `translator/_chatgpt.py` | `winform/chatgpt.py` |
| Dịch | 1 | Custom Translation API | `translator/_transapi.py` | `winform/transapi.py` |

Hai kênh *Custom API* là chỗ để trỏ tới Loli 2.0 và Loly 3.5 nếu muốn dùng chúng từ app desktop.

## Lưu ý quan trọng về ID kênh

Giao diện dùng **vị trí trong danh sách làm ID kênh** (`recogn_type.currentIndex()` tra thẳng vào
`_ID_NAME_DICT`). Vì vậy sau khi tỉa, toàn bộ ID đã được **đánh số lại liên tục từ 0**.

Hệ quả cần biết:

- File `videotrans/params.json` cũ (nếu có) đang lưu ID theo bảng cũ. Ví dụ `recogn_type: 11` từng
  là OpenAI API, giờ vượt ngoài phạm vi. **Xoá `videotrans/params.json` trước khi chạy lần đầu.**
- Khi thêm lại một kênh, phải chèn ID sao cho dãy vẫn liên tục và thứ tự `_ID_NAME_DICT` khớp với
  thứ tự hiển thị, nếu không giao diện sẽ gọi nhầm kênh.
- Cẩn thận với `if not channel_type` — ID 0 giờ là một kênh thật (ChatGPT, OpenAI STT, OpenAI TTS),
  `not 0` là `True` nên sẽ nuốt mất kênh đó. Dùng `is None`.

## Đã xoá những gì

**Model chạy tại máy** — faster-whisper, openai-whisper, whisper.cpp, Whisper.NET, FunASR,
FireRedASR, Dolphin, Omnilingual, Parakeet, Qwen-ASR, MOSS-Diarize, HuggingFace ASR, F5-TTS,
CosyVoice, ChatTTS, ChatterBox, GPT-SoVITS, Index-TTS, Kokoro, Higgs, Confucius, MOSS-TTS,
ZipVoice, Piper, VITS, Supertonic, VoxCPM, Spark, OmniVoice, M2M100, Hy-MT2.

**Kênh API của nhà cung cấp khác** — Deepgram, ElevenLabs, Google, Azure, Gemini, 302.AI, CAMB AI,
Qwen/Ali-Bailian, VolcEngine/豆包, XiaoMi, Zhipu GLM, MiniMax, SiliconFlow, OpenRouter, LiteLLM,
DeepSeek, DeepL, DeepLX, Baidu, Tencent, LibreTranslate, X.AI, Fish TTS, clone-voice.

**Kèm theo**: cửa sổ cấu hình (`winform/`), lớp giao diện (`ui/`), mục menu, khoá tham số trong
`configure/_app_params.py`, hằng số model trong `configure/contants.py`, bộ chạy model trong
`process/`, thư mục `videotrans/external/`, `videotrans/confuciustts/`, `videotrans/mosstts/`,
`videotrans/moss_transcribe_diarize/`, `videotrans/voicejson/`, `videotrans/codes/` và `f5-tts/`.

Tổng cộng **299 file bị xoá, 34 file được sửa** (so với commit `b77d60e2`).

## Những gì vẫn giữ

Toàn bộ phần lõi không phụ thuộc kênh: luồng task 8 bước (`videotrans/task/`), căn chỉnh
âm-hình (`_rate.py`), VAD, tách nhạc nền (UVR), giảm nhiễu, khôi phục dấu câu, phân biệt người nói,
các công cụ phụ (ghép/tách video-audio-phụ đề, đóng dấu, chuyển định dạng), giao diện desktop,
WebUI Gradio và CLI.

## Chạy

```bash
uv sync                 # cài dependency (xem cảnh báo bên dưới)
uv run sp.py            # giao diện desktop
uv run webui.py         # WebUI Gradio, cổng 7860
uv run cli.py --help    # dòng lệnh
```

> **Cảnh báo**: `pyproject.toml` và `uv.lock` vẫn khai đầy đủ dependency của bản gốc, gồm cả torch,
> faster-whisper, edge-tts… cho code đã xoá. Chưa tỉa vì cần chạy lại `uv lock` trên Python 3.10 và
> việc đó không kiểm chứng được trong môi trường hiện tại. `uv sync` vì thế vẫn tải rất nặng.
> Web app ở `webapp/` **không** dùng chung môi trường này.

Ví dụ CLI với các kênh còn lại:

```bash
uv run cli.py --task stt   --name "demo.mp4" --recogn_type 0
uv run cli.py --task tts   --name "demo.srt" --tts_type 0 --voice_role "alloy"
uv run cli.py --task trans --name "demo.srt" --translate_type 0
```
