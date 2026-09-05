"""Tiện ích cho phụ đề SRT: sinh, đọc và hậu xử lý sau khi nhận dạng."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Dict, List, Optional, Tuple

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
        cleaned_item = dict(it)
        cleaned_item.update(make_item(len(cleaned) + 1, it["start_ms"], it["end_ms"], text))
        cleaned.append(cleaned_item)
    for i in range(1, len(cleaned)):
        if cleaned[i - 1]["end_ms"] > cleaned[i]["start_ms"]:
            cleaned[i - 1]["end_ms"] = cleaned[i]["start_ms"]
    return [it for it in cleaned if it["end_ms"] > it["start_ms"]]


# Các tiếng "ậm ừ" mà ASR hay bịa ra khi phải nhận dạng một đoạn chỉ có nhạc nền
# hoặc tiếng động. Loli thường kèm theo việc đoán nhầm luôn cả ngôn ngữ.
FILLER_CJK = set("嗯啊哦噢呃唉哎呀咦哼嘿唔呐嗨嗐诶欸嘛喔呦")
FILLER_LATIN = {"uh", "uhh", "um", "umm", "ah", "ahh", "oh", "ohh", "mm", "mmm",
                "hmm", "hm", "huh", "eh", "er", "erm", "mhm", "hmph"}


def _script_of(text: str) -> str:
    """Hệ chữ chiếm đa số trong chuỗi: 'cjk', 'latin' hoặc 'other'."""
    counts: Counter = Counter()
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if "CJK" in name or "HIRAGANA" in name or "KATAKANA" in name or "HANGUL" in name:
            counts["cjk"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    return counts.most_common(1)[0][0] if counts else "other"


def _core_text(text: str) -> str:
    """Bỏ dấu câu và khoảng trắng, chỉ giữ phần chữ."""
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def _is_filler_only(text: str) -> bool:
    """Chuỗi chỉ gồm các tiếng ậm ừ, không mang nội dung gì."""
    core = _core_text(text)
    if not core:
        return True
    if _script_of(core) == "cjk":
        return all(ch in FILLER_CJK for ch in core)
    words = [w for w in re.split(r"[^0-9A-Za-zÀ-ÿ]+", text.lower()) if w]
    return bool(words) and all(w in FILLER_LATIN for w in words)


def drop_hallucinations(items: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Bỏ các dòng ASR bịa ra khi nhận vào nhạc nền / tiếng động.

    VAD chỉ cắt theo năng lượng nên vẫn lọt các đoạn chỉ có nhạc. Gặp đoạn không
    có tiếng người, Loli không trả về rỗng mà đoán bừa - thường là một tiếng ậm ừ
    kèm ngôn ngữ sai (video tiếng Anh mà ra `嗯。` / `language: Chinese`).

    Hai dấu hiệu đủ chắc để xoá, cả hai đều đòi *khác hệ chữ với phần còn lại của
    video* nên tiếng ậm ừ có thật trong đúng ngôn ngữ nguồn vẫn được giữ:

    A. Dòng chỉ gồm tiếng ậm ừ VÀ viết bằng hệ chữ khác đa số.
    B. Từ 3 dòng liên tiếp trở lên có nội dung giống hệt nhau và ngắn - kiểu lặp
       vô hạn kinh điển của ASR khi gặp đoạn câm.

    Trả về (dòng giữ lại, dòng bị bỏ). Dòng bị bỏ kèm khoá `_drop_reason`.
    """
    if not items:
        return [], []

    majority_script = _script_of(" ".join(it.get("text", "") for it in items))
    lang_counts = Counter((it.get("_language") or "").strip().lower()
                          for it in items if (it.get("text") or "").strip())
    lang_counts.pop("", None)
    majority_lang = lang_counts.most_common(1)[0][0] if lang_counts else ""

    flagged: List[Optional[str]] = [None] * len(items)

    for i, it in enumerate(items):
        text = (it.get("text") or "").strip()
        if not text:
            continue
        if _is_filler_only(text) and _script_of(text) != majority_script:
            lang = (it.get("_language") or "").strip().lower()
            detail = f", ngôn ngữ {lang or '?'} != {majority_lang or '?'}" if lang != majority_lang else ""
            flagged[i] = f"ậm ừ khác hệ chữ{detail}"

    # B. chuỗi lặp: giữ dòng đầu, bỏ các dòng sau
    run_start = 0
    for i in range(1, len(items) + 1):
        same = (i < len(items)
                and _core_text(items[i].get("text", "")) == _core_text(items[run_start].get("text", ""))
                and _core_text(items[i].get("text", "")))
        if same:
            continue
        run_len = i - run_start
        if run_len >= 3 and len(_core_text(items[run_start].get("text", ""))) <= 15:
            for j in range(run_start + 1, i):
                flagged[j] = flagged[j] or f"lặp {run_len} lần liên tiếp"
        run_start = i

    kept, dropped = [], []
    for it, reason in zip(items, flagged):
        if reason:
            item = dict(it)
            item["_drop_reason"] = reason
            dropped.append(item)
        else:
            kept.append(it)
    # Không bao giờ xoá sạch: nếu mọi dòng đều bị gắn cờ thì tin ASR hơn tin luật
    if not kept:
        return items, []
    for i, it in enumerate(kept, 1):
        it["line"] = i
    return kept, dropped


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
            if prev.get("_language") != it.get("_language"):
                prev["_language"] = "auto"
        else:
            out.append(dict(it))
    # câu cuối quá ngắn -> nhập vào câu trước
    if len(out) > 1 and (out[-1]["end_ms"] - out[-1]["start_ms"]) < min_ms // 2:
        if out[-1]["end_ms"] - out[-2]["start_ms"] <= max_ms * 1.5:
            out[-2]["end_ms"] = out[-1]["end_ms"]
            out[-2]["text"] = f"{out[-2]['text']}{join_flag}{out[-1]['text']}".strip()
            if out[-2].get("_language") != out[-1].get("_language"):
                out[-2]["_language"] = "auto"
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
