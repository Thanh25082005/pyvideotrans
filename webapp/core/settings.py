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
    },
    "stt": {
        "base_url": "https://studio.evomlabs.com",
        "api_key": "",
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
        # Kéo giãn tối đa bằng atempo, chỉ dùng khi chỉnh speed của TTS vẫn chưa đủ
        "max_audio_speed": 1.6,
        # Lệch dưới ngưỡng này thì không ép tốc độ, tránh làm giọng méo vô ích
        "fit_tolerance_ms": 200,
        # Nhịp thở chừa lại giữa hai câu liền nhau
        "gap_reserve_ms": 120,
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
