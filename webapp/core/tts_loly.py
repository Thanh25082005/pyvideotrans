"""Client cho Loly 3.5 Text-to-Speech (OriAgent Voice).

Contract: .claude/api-tts.md
- POST {base}/api/v1/tts/bytes    -> trả thẳng binary audio (dùng cho lồng tiếng)
- GET  {base}/api/v1/usage        -> quota còn lại
- GET  {base}/api/v1/keys         -> liệt kê voice mà account key với tới được
- POST {base}/api/v1/voices       -> nhân bản giọng từ một đoạn audio mẫu
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

import httpx

MAX_TEXT_LEN = 5000
FATAL_CODES = {
    "INVALID_API_KEY", "PERMISSION_DENIED", "VOICE_ID_REQUIRED", "VOICE_NOT_ALLOWED",
    "ACCOUNT_DISABLED", "VOICE_NOT_FOUND", "VOICE_LIMIT_REACHED", "KEY_CREDIT_EXCEEDED",
    "QUOTA_EXCEEDED", "TEXT_TOO_LONG", "INVALID_LANGUAGE", "INVALID_FORMAT",
    "VALIDATION_FAILED",
}


class TtsError(RuntimeError):
    def __init__(self, message: str, code: str = "", status: int = 0, fatal: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.fatal = fatal


def _parse_error(response: httpx.Response) -> TtsError:
    code, message = "", response.text[:300]
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or message)
            details = error.get("details")
            if isinstance(details, list) and details:
                message += " | " + "; ".join(
                    f"{d.get('field')}:{d.get('code')}" for d in details if isinstance(d, dict))
    except ValueError:
        pass
    fatal = code in FATAL_CODES or response.status_code in (400, 401, 403, 404)
    return TtsError(f"[{response.status_code}] {code or 'TTS_ERROR'}: {message}",
                    code=code, status=response.status_code, fatal=fatal)


class LolyTTS:
    def __init__(self, base_url: str, api_key: str, voice_id: str = "", timeout: float = 300.0):
        self.base_url = (base_url or "https://studio.evomlabs.com").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.voice_id = (voice_id or "").strip()
        self.timeout = timeout

    @property
    def is_account_key(self) -> bool:
        return self.api_key.startswith("vc_ak_live_")

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        return httpx.Client(timeout=timeout or self.timeout, follow_redirects=True)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _payload(self, text: str, language: str, speed: float, dit_steps: int, fmt: str,
                 duration: Optional[float] = None) -> dict:
        body = {
            "text": text,
            "language": language or "auto",
            "format": fmt,
            "cfg_value": 2.0,
            "dit_steps": max(0, min(int(dit_steps), 64)),
        }
        if duration is not None:
            if not 0.5 <= float(duration) <= 30.0:
                raise TtsError("duration phải nằm trong khoảng 0.5–30.0 giây", code="BAD_REQUEST", fatal=True)
            # Theo contract, duration ghi đè speed. Tắt hậu xử lý để pipeline tự
            # cắt silence và đo lại thời lượng thực tế.
            body["duration"] = round(float(duration), 3)
            body["postprocess_output"] = False
        else:
            body["speed"] = max(0.5, min(float(speed), 1.5))
        if self.is_account_key:
            if not self.voice_id:
                raise TtsError("Account key vc_ak_live_* bắt buộc phải có voice_id", code="VOICE_ID_REQUIRED", fatal=True)
            body["voice_id"] = self.voice_id
        elif self.voice_id:
            body["voice_id"] = self.voice_id
        return body

    def synthesize(self, text: str, out_path: str | Path, language: str = "auto",
                   speed: float = 1.0, dit_steps: int = 10, fmt: str = "wav",
                   duration: Optional[float] = None,
                   retries: int = 3) -> str:
        text = (text or "").strip()
        if not text:
            raise TtsError("Không có text để tổng hợp giọng nói", fatal=True)
        if not self.api_key:
            raise TtsError("Chưa cấu hình TTS API key", fatal=True)
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN]

        url = f"{self.base_url}/api/v1/tts/bytes"
        payload = self._payload(text, language, speed, dit_steps, fmt, duration=duration)
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                with self._client() as client:
                    response = client.post(url, headers=self._headers(), json=payload)
                if response.status_code == 200 and not response.headers.get(
                        "content-type", "").startswith("application/json"):
                    Path(out_path).write_bytes(response.content)
                    return str(out_path)
                error = _parse_error(response)
                if error.fatal:
                    raise error
                last_error = error
            except httpx.HTTPError as exc:
                last_error = TtsError(f"Lỗi kết nối TTS: {exc}")
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise last_error if last_error else TtsError("Tổng hợp giọng nói thất bại")

    def usage(self) -> dict:
        with self._client(60) as client:
            response = client.get(f"{self.base_url}/api/v1/usage", headers=self._headers())
        if response.status_code != 200:
            raise _parse_error(response)
        return response.json().get("data", {})

    def list_voices(self) -> List[dict]:
        """Chỉ account key có quyền key management mới liệt kê được voice."""
        with self._client(60) as client:
            response = client.get(f"{self.base_url}/api/v1/keys", headers=self._headers())
        if response.status_code != 200:
            raise _parse_error(response)
        voices, seen = [], set()
        for item in response.json().get("data", {}).get("keys", {}).get("tts", []) or []:
            voice = item.get("voice") or {}
            vid = voice.get("id")
            if vid and vid not in seen:
                seen.add(vid)
                voices.append({"id": vid, "name": voice.get("name") or vid})
        return voices

    def clone_voice(self, audio_path: str | Path, name: str, gender: str = "other",
                    description: str = "") -> dict:
        """Tạo voice clone từ đoạn audio mẫu. Cần account key vc_ak_live_*."""
        if not self.is_account_key:
            raise TtsError("Nhân bản giọng cần account key dạng vc_ak_live_*", fatal=True)
        path = Path(audio_path)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 10:
            raise TtsError(f"File mẫu {size_mb:.1f} MiB vượt giới hạn 10 MiB", fatal=True)
        with self._client() as client, path.open("rb") as fh:
            response = client.post(
                f"{self.base_url}/api/v1/voices",
                headers=self._headers(),
                files={"file": (path.name, fh, "audio/wav")},
                data={"name": name[:64], "consent": "true", "gender": gender,
                      "description": description},
            )
        if response.status_code not in (200, 201):
            raise _parse_error(response)
        data = response.json().get("data", {})
        return {
            "voice_id": data.get("voice_id", ""),
            "name": data.get("name", ""),
            "deduped": bool(data.get("deduped")),
            "voice_key": (data.get("api_key") or {}).get("key", ""),
        }

    def check_key(self) -> dict:
        if not self.api_key:
            return {"ok": False, "message": "Chưa nhập TTS API key"}
        try:
            data = self.usage()
        except TtsError as exc:
            return {"ok": False, "message": str(exc)}
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"Không kết nối được {self.base_url}: {exc}"}
        key_info = data.get("key", {}) or {}
        parts = [f"kind={key_info.get('kind', '?')}"]
        if data.get("remaining") is not None:
            parts.append(f"account còn {data['remaining']} ký tự")
        if key_info.get("credit_remaining") is not None:
            parts.append(f"key còn {key_info['credit_remaining']} ký tự")
        if self.is_account_key and not self.voice_id:
            parts.append("cần nhập voice_id vì đây là account key")
        return {"ok": True, "message": "TTS key hợp lệ - " + ", ".join(parts)}
