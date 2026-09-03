"""Client cho Loli 2.0 Speech-to-Text (OriAgent Voice).

Contract: .claude/api-stt.md
- POST {base}/api/v1/stt/transcriptions  (multipart: audio, language) + Bearer key
- GET  {base}/api/public/v1/auth/check   (X-API-Key) - có deployment không expose
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import httpx

MAX_FILE_MB = 100
# Lỗi không nên retry: sai key, hết quota, sai định dạng
FATAL_CODES = {
    "INVALID_API_KEY", "unauthorized", "FORBIDDEN", "PERMISSION_DENIED",
    "KEY_CREDIT_EXCEEDED", "QUOTA_EXCEEDED", "FILE_TOO_LARGE",
    "unsupported_audio_format", "SERVICE_NOT_CONFIGURED",
}


class SttError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0, fatal: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.fatal = fatal


def _parse_error(response: httpx.Response) -> SttError:
    code, message = "", response.text[:300]
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or message)
        elif isinstance(body, dict) and body.get("message"):
            message = str(body["message"])
    except ValueError:
        pass
    fatal = code in FATAL_CODES or response.status_code in (401, 403, 413, 415)
    return SttError(f"[{response.status_code}] {code or 'STT_ERROR'}: {message}",
                    code=code, status=response.status_code, fatal=fatal)


class LoliSTT:
    def __init__(self, base_url: str, api_key: str, timeout: float = 300.0):
        self.base_url = (base_url or "https://studio.evomlabs.com").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, follow_redirects=True)

    def transcribe(self, audio_path: str | Path, language: str = "auto", retries: int = 3) -> dict:
        path = Path(audio_path)
        if not path.exists():
            raise SttError(f"Không tìm thấy file audio: {path}", fatal=True)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            raise SttError(f"File {path.name} nặng {size_mb:.1f} MiB, vượt giới hạn {MAX_FILE_MB} MiB", fatal=True)
        if not self.api_key:
            raise SttError("Chưa cấu hình STT API key", fatal=True)

        url = f"{self.base_url}/api/v1/stt/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                with self._client() as client, path.open("rb") as fh:
                    response = client.post(
                        url,
                        headers=headers,
                        files={"audio": (path.name, fh, "audio/wav")},
                        data={"language": language or "auto"},
                    )
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "text": (data.get("text") or "").strip(),
                        "language": data.get("language") or language,
                        "model": data.get("model", ""),
                    }
                error = _parse_error(response)
                if error.fatal:
                    raise error
                last_error = error
            except httpx.HTTPError as exc:
                last_error = SttError(f"Lỗi kết nối STT: {exc}")
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise last_error if last_error else SttError("Nhận dạng thất bại không rõ nguyên nhân")

    def check_key(self) -> dict:
        """Kiểm tra key. Ưu tiên auth/check, nếu route không tồn tại thì thử transcribe file rỗng."""
        if not self.api_key:
            return {"ok": False, "message": "Chưa nhập STT API key"}
        try:
            with self._client() as client:
                response = client.get(
                    f"{self.base_url}/api/public/v1/auth/check",
                    headers={"X-API-Key": self.api_key, "Authorization": f"Bearer {self.api_key}"},
                    timeout=30.0,
                )
            if response.status_code == 200:
                return {"ok": True, "message": "STT key hợp lệ (auth/check)"}
            if response.status_code in (401, 403):
                return {"ok": False, "message": _parse_error(response).args[0]}
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"Không kết nối được {self.base_url}: {exc}"}
        return self._probe_transcription()

    def _probe_transcription(self) -> dict:
        """auth/check không khả dụng: gửi 0.5s im lặng để xác thực (transcript rỗng, ~0 ký tự)."""
        import tempfile

        from .ffmpeg import FFmpegError, run_ffmpeg

        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                run_ffmpeg(["-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                            "-t", "0.5", "-c:a", "pcm_s16le", tmp.name])
                result = self.transcribe(tmp.name, language="auto", retries=1)
            return {"ok": True, "message": f"STT key hợp lệ (model: {result.get('model') or 'Loli 2.0'})"}
        except SttError as exc:
            return {"ok": False, "message": str(exc)}
        except FFmpegError as exc:
            return {"ok": False, "message": f"Không tạo được file kiểm tra: {exc}"}
