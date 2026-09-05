"""Lưu/đọc cấu hình API key. File nằm ở webapp/data/config.json (chmod 600)."""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
CONFIG_PATH = DATA_DIR / "config.json"

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "openai": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "temperature": 0.3,
        "batch_size": 20,
        # Trần ký tự của khối transcript gửi kèm mỗi lô để model hiểu ngữ cảnh.
        # Dài hơn thì chỉ gửi cửa sổ bao quanh lô đang dịch. Đặt 0 để tắt hẳn.
        "context_chars": 12000,
        # Thuật ngữ bắt buộc giữ nguyên, ngoài các nhóm đã ghi sẵn trong prompt
        "keep_terms": [],
        # Chỉ thị dịch tự do do người dùng viết: xưng hô, giọng văn, từ cấm dịch...
        # Được chèn vào prompt dưới dạng khối <USER_INSTRUCTIONS>, đè lên các quy
        # tắc về văn phong nhưng KHÔNG đè được cấu trúc đầu ra. Trần 4000 ký tự.
        "instruction": "",
        # Bảng giá để quy token ra tiền. Đơn vị: USD cho 1 TRIỆU token.
        # Số token là con số thật OpenAI trả về nên luôn chính xác; còn tiền thì
        # chỉ đúng khi bảng này khớp bảng giá hiện hành - OpenAI đổi giá và thêm
        # model liên tục, nên HÃY TỰ ĐỐI CHIẾU openai.com/api/pricing rồi sửa lại.
        # Model không có trong bảng thì chỉ hiện số token, không hiện tiền.
        "pricing": {
            "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
            "gpt-4o": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
            "gpt-4.1-mini": {"input": 0.40, "cached_input": 0.10, "output": 1.60},
            "gpt-4.1": {"input": 2.00, "cached_input": 0.50, "output": 8.00},
            "gpt-4.1-nano": {"input": 0.10, "cached_input": 0.025, "output": 0.40},
        },
    },
    "resegment": {
        # Tách khối ASR thành từng câu, lấy mốc thật từ word-level timestamps
        "enabled": True,
        # Mảnh ngắn hơn ngưỡng này thì nhập lại vào câu trước
        "min_piece_ms": 400,
        "margin_ms": 40,
        # Nhờ LLM chấm câu cho dòng dài mà transcript không có dấu kết nào
        "llm_assist": True,
        "llm_min_ms": 6000,
    },
    "stt": {
        "base_url": "https://studio.evomlabs.com",
        "api_key": "",
    },
    "separate": {
        # Tách giọng khỏi nhạc nền bằng Hybrid Demucs trước khi VAD/ASR.
        # Đây là cách duy nhất thật sự chắc để nhạc không bị nhận nhầm là thoại;
        # đặc trưng phổ (pipeline.music_filter) đã đo là không phân biệt được.
        # Model chạy trong .aligner-venv, không chạy được thì tự quay về đường cũ.
        "enabled": True,
        "base_url": "http://127.0.0.1:8200",
        "timeout": 1800,
        # Âm lượng nhạc nền đã tách khi trộn vào bản lồng tiếng. Vì stem này
        # không còn giọng gốc nên để cao được, khác hẳn khi trộn nguyên audio gốc.
        "accompaniment_volume": 0.9,
        # MẶC ĐỊNH TẮT. amix chạy với normalize=0 nên số âm lượng đúng nghĩa
        # "giữ nguyên", nhưng khi nền và lồng tiếng cùng to thì có thể vượt trần.
        # Đo thực tế: alimiter KHÔNG trong suốt - nó lệch 86% số mẫu ngay cả khi
        # còn xa ngưỡng (có trễ lookahead ~10ms), tức là phá đúng cái ta muốn giữ.
        # Nên thay vì âm thầm nén, pipeline đếm mẫu chạm trần rồi cảnh báo để bạn
        # tự hạ âm lượng. Bật lên nếu bạn chấp nhận đánh đổi để khỏi clip.
        "mix_limiter": False,
    },
    "aligner": {
        "enabled": True,
        "base_url": "http://127.0.0.1:8200",
        "timeout": 300,
        # Ghi file output/align-debug.log: timestamp từng từ + bảng chèn timeline
        "debug_log": True,
        # Đẩy timestamp từng từ lên cả log job trên UI (rất dài, chỉ bật khi soi lỗi)
        "log_words_to_ui": False,
        # Cảnh báo khi aligner trả về mốc vượt quá độ dài clip quá ngưỡng này
        "overshoot_warn_ms": 150,
        # Nắn từ bị kéo dãn qua khoảng lặng: dài hơn stretch_min_ms VÀ gấp
        # stretch_factor lần độ dài đáng lẽ có (trung vị giây/ký tự của cả video)
        "fix_stretched_words": True,
        "stretch_factor": 4.0,
        "stretch_min_ms": 800,
    },
    "tts": {
        "base_url": "https://studio.evomlabs.com",
        "api_key": "",
        "voice_id": "",
        "speed": 1.0,
        "dit_steps": 16,
    },
    "pipeline": {
        # Tham số VAD - quyết định cách cắt câu trước khi gửi ASR
        "min_speech_ms": 1200,
        "max_speech_ms": 18000,
        "min_silence_ms": 400,
        # Trừ nền theo dải tần trước khi dò năng lượng: nhạc nền đều đều chìm vào
        # sàn, chỉ phần giọng nhô lên mới tính -> biên câu bám đúng lúc nói.
        "subtract_music_floor": True,
        # Chấm điểm "giống giọng nói" cho từng cửa sổ 1,2s rồi vứt vùng chỉ có nhạc.
        # MẶC ĐỊNH TẮT: đo trên phim thật (nhạc phim + hiệu ứng) thì điểm của nhạc
        # (0,66-0,87) chồng hoàn toàn lên điểm của thoại (0,65-0,86), không ngưỡng
        # nào cắt được. Cách giải đúng là tách vocal (separate_audio) chứ không
        # phải đặc trưng phổ. Giữ lại code để soi bằng `python -m core.vad`.
        "music_filter": False,
        # Ngưỡng điểm 0..1. Nhạc thuần thường 0,05-0,20; thoại 0,75-0,99.
        # Tăng lên nếu nhạc vẫn lọt, giảm xuống nếu thoại bị cắt mất.
        "speech_score_min": 0.45,
        # Khoảng nhạc phải dài hơn chừng này mới cắt đôi đoạn thoại
        "music_gap_ms": 700,
        # Kéo giãn tối đa bằng atempo, chỉ dùng khi chỉnh speed của TTS vẫn chưa đủ
        "max_audio_speed": 1.6,
        # Lệch dưới ngưỡng này thì không ép tốc độ, tránh làm giọng méo vô ích
        "fit_tolerance_ms": 200,
        # Nhịp thở chừa lại giữa hai câu liền nhau
        "gap_reserve_ms": 120,
        # Trần tuyệt đối: câu lồng tiếng không được đọc quá mốc giọng gốc dứt thêm
        # chừng này. Khoảng lặng phía sau là lúc nhân vật đã ngậm miệng, không mượn được.
        "max_overrun_ms": 500,
        # Khi speed native của TTS đã kịch mà câu vẫn tràn, atempo được ép tới đây
        "hard_max_audio_speed": 2.5,
        "binary_iterations": 4,
        # Số request song song tới STT / TTS
        "stt_concurrency": 3,
        "tts_concurrency": 3,
    },
}


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def _merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    _ensure_dirs()
    with _lock:
        if not CONFIG_PATH.exists():
            return deepcopy(DEFAULT_CONFIG)
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return deepcopy(DEFAULT_CONFIG)
    return _merge(DEFAULT_CONFIG, saved if isinstance(saved, dict) else {})


def save_config(patch: dict) -> dict:
    """Ghi đè một phần cấu hình, giữ nguyên các khoá không gửi lên."""
    _ensure_dirs()
    current = load_config()
    merged = _merge(current, patch or {})
    # chỉ giữ các nhóm hợp lệ
    merged = {k: v for k, v in merged.items() if k in DEFAULT_CONFIG}
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
    return merged


def job_dir(job_id: str) -> Path:
    path = JOBS_DIR / job_id
    (path / "cache").mkdir(parents=True, exist_ok=True)
    (path / "output").mkdir(parents=True, exist_ok=True)
    return path
