"""Tiện ích cho phụ đề SRT: sinh, đọc và hậu xử lý sau khi nhận dạng."""
from __future__ import annotations

import re
from typing import Dict, List

PUNC_END = "。．.？?！!"
PUNC_HALF = "，,、；;：:"
NON_WORD = re.compile(r"^[\s\W_]+$", re.UNICODE)
TIME_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def ms_to_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def time_to_ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return int(hours) * 3600000 + int(minutes) * 60000 + int(seconds) * 1000 + int(millis.ljust(3, "0")[:3])


def make_item(line: int, start_ms: int, end_ms: int, text: str) -> Dict:
    return {"line": line, "start_ms": int(start_ms), "end_ms": int(end_ms), "text": text.strip()}


def to_srt(items: List[Dict]) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        blocks.append(f"{i}\n{ms_to_time(it['start_ms'])} --> {ms_to_time(it['end_ms'])}\n{it['text'].strip()}")
    return "\n\n".join(blocks) + "\n"


def parse_srt(text: str) -> List[Dict]:
    items: List[Dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        time_line_idx = 0 if TIME_RE.search(lines[0]) else (1 if len(lines) > 1 and TIME_RE.search(lines[1]) else -1)
        if time_line_idx < 0:
            continue
        match = TIME_RE.search(lines[time_line_idx])
        content = " ".join(lines[time_line_idx + 1:]).strip()
        items.append(make_item(
            len(items) + 1,
            time_to_ms(*match.groups()[:4]),
            time_to_ms(*match.groups()[4:]),
            content,
        ))
    return items


def clean_and_fix(items: List[Dict]) -> List[Dict]:
    """Bỏ dòng rỗng/chỉ có dấu câu và sửa các mốc thời gian chồng lấn."""
    cleaned: List[Dict] = []
    for it in items:
        text = (it.get("text") or "").strip()
        if not text or NON_WORD.match(text):
            continue
        cleaned.append(make_item(len(cleaned) + 1, it["start_ms"], it["end_ms"], text))
    for i in range(1, len(cleaned)):
        if cleaned[i - 1]["end_ms"] > cleaned[i]["start_ms"]:
            cleaned[i - 1]["end_ms"] = cleaned[i]["start_ms"]
    return [it for it in cleaned if it["end_ms"] > it["start_ms"]]


def merge_short(items: List[Dict], min_ms: int = 1000, max_ms: int = 18000,
                join_flag: str = " ") -> List[Dict]:
    """Gộp các câu quá ngắn vào câu liền kề (rút gọn từ BaseRecogn._merge_sub)."""
    if len(items) < 2:
        return items
    out: List[Dict] = [dict(items[0])]
    for it in items[1:]:
        prev = out[-1]
        prev_dur = prev["end_ms"] - prev["start_ms"]
        gap = it["start_ms"] - prev["end_ms"]
        prev_open = prev["text"][-1] not in PUNC_END if prev["text"] else True
        would_be = it["end_ms"] - prev["start_ms"]
        if (prev_dur < min_ms or (prev_open and prev_dur + gap < min_ms)) and gap < 800 and would_be <= max_ms:
            prev["end_ms"] = it["end_ms"]
            prev["text"] = f"{prev['text']}{join_flag}{it['text']}".strip()
        else:
            out.append(dict(it))
    # câu cuối quá ngắn -> nhập vào câu trước
    if len(out) > 1 and (out[-1]["end_ms"] - out[-1]["start_ms"]) < min_ms // 2:
        if out[-1]["end_ms"] - out[-2]["start_ms"] <= max_ms * 1.5:
            out[-2]["end_ms"] = out[-1]["end_ms"]
            out[-2]["text"] = f"{out[-2]['text']}{join_flag}{out[-1]['text']}".strip()
            out.pop()
    for i, it in enumerate(out, 1):
        it["line"] = i
    return out


def align_by_time(source_items: List[Dict], translated: List[Dict]) -> List[Dict]:
    """Nếu số dòng dịch không khớp, khớp lại theo mốc thời gian (check_target_sub)."""
    if len(source_items) == len(translated):
        merged = []
        for src, dst in zip(source_items, translated):
            merged.append(make_item(src["line"], src["start_ms"], src["end_ms"], dst["text"]))
        return merged
    by_time = {(it["start_ms"], it["end_ms"]): it["text"] for it in translated}
    out = []
    for i, src in enumerate(source_items):
        text = by_time.get((src["start_ms"], src["end_ms"]))
        if text is None:
            text = translated[i]["text"] if i < len(translated) else ""
        out.append(make_item(src["line"], src["start_ms"], src["end_ms"], text))
    return out
