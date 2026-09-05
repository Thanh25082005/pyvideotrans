"""HTTP client cho endpoint tách giọng/nhạc của aligner service.

Tách vocal là cách duy nhất thật sự chắc để nhạc nền không bị nhận nhầm là
thoại: VAD/ASR chạy trên stem giọng đã sạch nhạc, còn stem nhạc nền được giữ
lại để trộn vào bản lồng tiếng mà không kéo theo giọng gốc.

Model (Hybrid Demucs) nằm trong .aligner-venv cùng torch, webapp chính vẫn chỉ
cần numpy. Không dùng được thì pipeline tự quay về đường cũ.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import httpx


class SeparateError(RuntimeError):
    pass


class SeparatorClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8200", timeout: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=5.0)
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except Exception:
            return False

    def separate(self, source: str | Path, vocals: str | Path,
                 accompaniment: Optional[str | Path] = None) -> Dict:
        """Tách file thành hai stem. Đường dẫn tuyệt đối, cùng máy, không upload."""
        payload = {
            "input_path": str(Path(source).resolve()),
            "vocals_path": str(Path(vocals).resolve()),
            "accompaniment_path": str(Path(accompaniment).resolve()) if accompaniment else "",
        }
        try:
            response = httpx.post(f"{self.base_url}/separate", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SeparateError(f"Không gọi được service tách nhạc: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
            raise SeparateError(f"[{response.status_code}] {detail}")
        return response.json()
