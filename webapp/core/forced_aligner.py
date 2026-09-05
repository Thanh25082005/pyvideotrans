"""HTTP client cho service Qwen3 Forced Aligner chạy nội bộ."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import httpx


LANGUAGE_NAMES = {
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "ru": "Russian",
    "es": "Spanish",
}


class ForcedAlignerError(RuntimeError):
    pass


def supported_language(code: str) -> Optional[str]:
    raw = (code or "").strip().lower().replace("_", "-")
    if raw in {"en-vi", "ko-en", "ja-en"}:
        return None
    normalized = raw.split("-")[0]
    return LANGUAGE_NAMES.get(normalized)


class QwenForcedAlignerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8200", timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> Dict:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=5.0)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ForcedAlignerError(f"Qwen aligner chưa sẵn sàng: {exc}") from exc

    def align(self, audio_path: str | Path, text: str, language: str) -> List[Dict]:
        path = Path(audio_path)
        if not path.is_file():
            raise ForcedAlignerError(f"Không tìm thấy audio để align: {path}")
        try:
            with path.open("rb") as audio:
                response = httpx.post(
                    f"{self.base_url}/align",
                    files={"audio": (path.name, audio, "audio/wav")},
                    data={"text": text, "language": language},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ForcedAlignerError(f"Forced alignment thất bại: {exc}") from exc
        words = body.get("words", []) if isinstance(body, dict) else []
        return [word for word in words if isinstance(word, dict)]
