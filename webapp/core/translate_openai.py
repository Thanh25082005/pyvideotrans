"""Dịch phụ đề bằng OpenAI Chat Completions.

Prompt rút gọn từ videotrans/prompts/srt/chatgpt.txt: dịch cho lồng tiếng nên
phải ngắn gọn, giữ ánh xạ 1-1 theo từng dòng, giọng nói đời thường.
Mỗi dòng được đánh số [n] để ghép lại chính xác kể cả khi model trả thiếu dòng.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Dict, List, Optional

import httpx

SYSTEM_PROMPT = "You are a top-tier subtitle translation engine for AI dubbing."

USER_PROMPT = """# ROLE
Translate the numbered subtitle lines inside <INPUT> into {lang}.

# RULES
1. DUBBING-SAFE: the result is read aloud by a TTS voice inside the same time slot as the
   original line. Use the shortest natural spoken phrasing; drop filler words; never add
   explanations, notes or transliterations.
2. TIME BUDGET: a marker `[n|3.2s]` means that line must be spoken within 3.2 seconds.
   A TTS voice reads roughly 14 characters/second for Latin scripts and 5 for Chinese,
   Japanese or Thai. When your draft would not fit, compress it: cut adjectives, pronouns
   and politeness padding until it fits. Never pad a short line to fill its budget.
3. STRICT 1-TO-1 MAPPING: output exactly one line per index, keeping the same number in the
   `[n]` marker and the same order. Never merge, split, reorder or skip lines.
4. FRAGMENTS STAY FRAGMENTS: a line cut mid-sentence must stay an incomplete fragment; do
   not move words between lines to fix grammar.
5. SPOKEN REGISTER: everyday conversational {lang}, matching the tone of the source.
6. Keep numbers, names and units. If a line is already in {lang}, return it unchanged.
7. PURE OUTPUT: only the translated lines inside <TRANSLATE_TEXT> tags, no markdown fences,
   no commentary.

# FORMAT
<TRANSLATE_TEXT>
[1] translated line 1
[2] translated line 2
</TRANSLATE_TEXT>

# TASK
Source language: {source_lang}. Target language: {lang}.

<INPUT>
{batch_input}
</INPUT>"""

LINE_RE = re.compile(r"^\s*\[(\d+)(?:\s*\|[^\]]*)?\]\s*(.*)$")


def _error_message(response: httpx.Response) -> str:
    """Rút gọn body lỗi của OpenAI thành một câu để hiển thị trên UI."""
    try:
        error = response.json().get("error") or {}
        message = error.get("message") or ""
        code = error.get("code") or error.get("type") or ""
        if message:
            return f"{code + ': ' if code else ''}{message}"[:300]
    except ValueError:
        pass
    return response.text[:300]


class TranslateError(RuntimeError):
    pass


class OpenAITranslator:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-4.1-mini", temperature: float = 0.3,
                 batch_size: int = 20, timeout: float = 300.0):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model or "gpt-4.1-mini"
        self.temperature = temperature
        self.batch_size = max(1, int(batch_size or 20))
        self.timeout = timeout
        self._drop_temperature = False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _chat(self, messages: List[dict], retries: int = 3) -> str:
        if not self.api_key:
            raise TranslateError("Chưa cấu hình OpenAI API key")
        url = f"{self.base_url}/chat/completions"
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            payload: Dict = {"model": self.model, "messages": messages}
            if not self._drop_temperature:
                payload["temperature"] = float(self.temperature)
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise TranslateError(f"OpenAI trả về rỗng: {str(data)[:200]}")
                    return (choices[0].get("message", {}).get("content") or "").strip()

                body = _error_message(response)
                if response.status_code == 400 and "temperature" in body and not self._drop_temperature:
                    # model dòng reasoning chỉ nhận temperature mặc định
                    self._drop_temperature = True
                    continue
                if response.status_code in (401, 403, 404):
                    raise TranslateError(f"[{response.status_code}] OpenAI: {body}")
                last_error = TranslateError(f"[{response.status_code}] OpenAI: {body}")
            except httpx.HTTPError as exc:
                last_error = TranslateError(f"Lỗi kết nối OpenAI: {exc}")
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        raise last_error if last_error else TranslateError("Dịch thất bại không rõ nguyên nhân")

    @staticmethod
    def _parse_reply(reply: str) -> Dict[int, str]:
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
        match = re.search(r"<TRANSLATE_TEXT>(.*?)</TRANSLATE_TEXT>", reply, re.S)
        body = match.group(1) if match else reply
        out: Dict[int, str] = {}
        current: Optional[int] = None
        for raw_line in body.splitlines():
            found = LINE_RE.match(raw_line)
            if found:
                current = int(found.group(1))
                out[current] = found.group(2).strip()
            elif current is not None and raw_line.strip():
                out[current] = f"{out[current]} {raw_line.strip()}".strip()
        return out

    @staticmethod
    def _marker(item: Dict, budgets: Optional[Dict[int, int]]) -> str:
        budget = (budgets or {}).get(item["line"])
        return f"[{item['line']}|{budget / 1000:.1f}s]" if budget else f"[{item['line']}]"

    def _translate_batch(self, batch: List[Dict], target_name: str, source_name: str,
                         budgets: Optional[Dict[int, int]] = None) -> Dict[int, str]:
        payload_lines = "\n".join(f"{self._marker(it, budgets)} {it['text'].strip()}" for it in batch)
        prompt = (USER_PROMPT
                  .replace("{lang}", target_name)
                  .replace("{source_lang}", source_name or "auto-detected")
                  .replace("{batch_input}", payload_lines))
        reply = self._chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        return self._parse_reply(reply)

    def translate(self, items: List[Dict], target_name: str, source_name: str = "",
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  budgets: Optional[Dict[int, int]] = None) -> List[Dict]:
        """Trả về bản sao items với text đã dịch (giữ nguyên mốc thời gian)."""
        results: Dict[int, str] = {}
        batches = [items[i:i + self.batch_size] for i in range(0, len(items), self.batch_size)]
        for index, batch in enumerate(batches, 1):
            try:
                translated = self._translate_batch(batch, target_name, source_name, budgets)
            except TranslateError as exc:
                if "401" in str(exc) or "403" in str(exc):
                    raise
                translated = {}
                if progress:
                    progress(index, len(batches), f"lô {index} lỗi ({exc}), sẽ dịch lại từng dòng")

            missing = [it for it in batch if not translated.get(it["line"], "").strip()]
            if missing and len(missing) <= max(3, len(batch) // 2):
                # thiếu vài dòng -> dịch bù riêng từng dòng
                for it in missing:
                    try:
                        single = self._translate_batch([it], target_name, source_name, budgets)
                        if single.get(it["line"], "").strip():
                            translated[it["line"]] = single[it["line"]]
                    except TranslateError:
                        pass
            elif missing:
                retry = self._translate_batch(batch, target_name, source_name, budgets)
                translated.update({k: v for k, v in retry.items() if v.strip()})

            for it in batch:
                results[it["line"]] = translated.get(it["line"], "").strip() or it["text"]
            if progress:
                progress(index, len(batches), f"đã dịch {min(index * self.batch_size, len(items))}/{len(items)} dòng")
        return [dict(it, text=results.get(it["line"], it["text"])) for it in items]

    def check_key(self) -> dict:
        if not self.api_key:
            return {"ok": False, "message": "Chưa nhập OpenAI API key"}
        try:
            reply = self._chat([{"role": "user", "content": "Reply with the single word: ok"}], retries=1)
        except TranslateError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": f"OpenAI OK - model {self.model} trả lời: {reply[:40]}"}
