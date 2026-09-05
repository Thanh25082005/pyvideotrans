"""Tách một dòng phụ đề thành nhiều câu, lấy mốc thời gian từ word-level timestamps.

Loli trả về nguyên một khối như "And you'll give me back my satchel. I promise."
kèm mốc VAD của cả khối. Đọc liền một mạch như vậy là nguồn gốc lệch tiếng: câu
"I promise" đáng lẽ bắt đầu ở 00:04:37 nhưng bị đọc ngay sau câu trước.

Ở đây cắt khối đó thành từng câu rồi lấy mốc thật của câu từ danh sách từ mà
Qwen3 Forced Aligner đã căn:

    And(0.16) ... satchel(1.60)  ->  câu 1: 00:04:35.15 -> 00:04:36.63
    I(2.08) ... promise(2.40)    ->  câu 2: 00:04:37.07 -> 00:04:37.51

Dấu câu trong transcript là tín hiệu chính. Khi ASR trả về một khối dài mà không
có dấu chấm nào thì mới nhờ LLM chấm câu hộ - xem `llm` trong `split_items`.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Dấu kết câu, gồm cả bản full-width của CJK
SENTENCE_END = "。．.？?！!…"
# Tách sau dấu kết câu; giữ dấu lại với câu phía trước
SPLIT_RE = re.compile(rf"(?<=[{re.escape(SENTENCE_END)}])\s+")
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm(text: str) -> str:
    """Chuẩn hoá để so khớp: bỏ dấu câu và khoảng trắng, hạ chữ thường."""
    return STRIP_RE.sub("", (text or "")).lower()


def sentences_of(text: str) -> List[str]:
    """Cắt câu theo dấu chấm. CJK không có khoảng trắng nên cắt ngay sau dấu."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if len(parts) > 1:
        return parts
    # CJK: "嗯。啊！" không có khoảng trắng sau dấu nên regex trên không ăn
    if CJK_RE.search(text):
        parts = [p.strip() for p in re.split(rf"(?<=[{re.escape(SENTENCE_END)}])", text) if p.strip()]
        if len(parts) > 1:
            return parts
    return [text]


def _snap_to_gap(words: Sequence[Dict], boundary: int, low: int, high: int, reach: int = 2) -> int:
    """Nhích ranh giới về chỗ ngắt hơi to nhất quanh đó.

    Giữa hai câu bao giờ cũng có khoảng lặng dài hơn giữa hai từ trong cùng câu,
    nên đây là cách rẻ tiền để bù cho sai số vài từ của bước ước lượng.
    """
    best, best_gap = boundary, -1.0
    for index in range(max(low + 1, boundary - reach), min(high, boundary + reach) + 1):
        gap = float(words[index]["start"]) - float(words[index - 1]["end"])
        if gap > best_gap:
            best, best_gap = index, gap
    return best


def _map_sentences(sentences: Sequence[str], words: Sequence[Dict]) -> Optional[List[Tuple[int, int]]]:
    """Chia danh sách từ thành từng nhóm ứng với mỗi câu.

    So khớp theo số ký tự đã chuẩn hoá chứ không theo số token: aligner hay tách
    hoặc gộp khác với transcript ("you'll" -> "you" + "ll"), đếm ký tự thì vẫn khớp.

    Aligner cũng có thể trả về ít từ hơn transcript - từ độ dài 0 đã bị loại - nên
    số ký tự cần luôn được quy đổi theo tỉ lệ trên số ký tự thực có, rồi ranh giới
    được nhích về khoảng lặng gần nhất.
    """
    lengths = [len(_norm(word.get("word", ""))) for word in words]
    available = sum(lengths)
    needs = [len(_norm(sentence)) for sentence in sentences]
    wanted = sum(needs)
    if not available or not wanted or len(words) < len(sentences):
        return None

    scale = available / wanted
    groups: List[Tuple[int, int]] = []
    cursor = 0
    for index, need in enumerate(needs):
        if need <= 0:
            continue
        remaining = len(sentences) - index - 1     # mỗi câu sau còn phải có ít nhất 1 từ
        limit = len(words) - remaining
        if cursor >= limit:
            return None
        start = cursor
        got = 0.0
        target = need * scale
        while cursor < limit and (got < target or lengths[cursor] == 0):
            got += lengths[cursor]
            cursor += 1
            if got >= target:
                break
        if index == len(needs) - 1:
            cursor = len(words)
        if cursor <= start:
            return None
        groups.append((start, cursor))
    if not groups:
        return None
    if cursor < len(words):
        groups[-1] = (groups[-1][0], len(words))

    # Nhích từng ranh giới về khoảng lặng to nhất quanh nó
    for index in range(len(groups) - 1):
        low = groups[index][0]
        high = groups[index + 1][1] - 1
        if high <= low + 1:
            continue
        snapped = _snap_to_gap(words, groups[index][1], low, high)
        groups[index] = (low, snapped)
        groups[index + 1] = (snapped, groups[index + 1][1])
    return [g for g in groups if g[1] > g[0]] or None


def _piece(item: Dict, line: int, text: str, chunk: Sequence[Dict],
           margin_ms: int, low_ms: int, high_ms: int) -> Dict:
    start_ms = max(low_ms, int(round(chunk[0]["start"] * 1000)) - margin_ms)
    end_ms = min(high_ms, int(round(chunk[-1]["end"] * 1000)) + margin_ms)
    piece = dict(item)
    piece.update({
        "line": line,
        "start_ms": start_ms,
        "end_ms": max(end_ms, start_ms + 1),
        "text": text.strip(),
        "source_words": list(chunk),
    })
    return piece


def _merge_into(target: Dict, extra: Dict, join_flag: str) -> None:
    target["end_ms"] = max(target["end_ms"], extra["end_ms"])
    target["text"] = f"{target['text']}{join_flag}{extra['text']}".strip()
    target["source_words"] = list(target.get("source_words", [])) + list(extra.get("source_words", []))


def split_item(item: Dict, min_piece_ms: int = 400, margin_ms: int = 40,
               join_flag: str = " ", sentences: Optional[Sequence[str]] = None) -> List[Dict]:
    """Cắt một dòng thành nhiều câu. Không cắt được thì trả lại nguyên dòng."""
    words = item.get("source_words") or []
    text = (item.get("text") or "").strip()
    if len(words) < 2 or not text:
        return [item]

    parts = list(sentences) if sentences else sentences_of(text)
    if len(parts) < 2:
        return [item]

    groups = _map_sentences(parts, words)
    if not groups or len(groups) < 2:
        return [item]

    low_ms, high_ms = item["start_ms"], item["end_ms"]
    pieces: List[Dict] = []
    for order, ((start, stop), sentence) in enumerate(zip(groups, parts)):
        chunk = words[start:stop]
        if not chunk:
            continue
        piece = _piece(item, item["line"], sentence, chunk, margin_ms, low_ms, high_ms)
        # Câu quá ngắn thì nhập lại vào câu trước, tách ra chỉ tổ vụn phụ đề
        if pieces and piece["end_ms"] - piece["start_ms"] < min_piece_ms:
            _merge_into(pieces[-1], piece, join_flag)
            continue
        pieces.append(piece)

    if len(pieces) < 2:
        return [item]
    # Không để câu trước đè lên câu sau
    for i in range(len(pieces) - 1):
        pieces[i]["end_ms"] = min(pieces[i]["end_ms"], pieces[i + 1]["start_ms"])
    return [p for p in pieces if p["end_ms"] > p["start_ms"]] or [item]


def split_items(items: List[Dict], min_piece_ms: int = 400, margin_ms: int = 40,
                join_flag: str = " ",
                llm: Optional[Callable[[Dict], Optional[List[str]]]] = None,
                llm_min_ms: int = 6000,
                log: Optional[Callable[[str, str], None]] = None) -> List[Dict]:
    """Cắt cả danh sách rồi đánh số lại.

    `llm(item) -> [câu, ...] | None` chỉ được gọi cho những dòng dài mà transcript
    không có dấu kết câu nào ở giữa - đúng chỗ mà cắt theo dấu chấm bó tay.
    """
    out: List[Dict] = []
    split_count = 0
    llm_used = 0
    for item in items:
        pieces = split_item(item, min_piece_ms, margin_ms, join_flag)
        if len(pieces) == 1 and llm and (item.get("source_words") or []):
            duration = item["end_ms"] - item["start_ms"]
            if duration >= llm_min_ms:
                try:
                    sentences = llm(item)
                except Exception as exc:                      # noqa: BLE001 - LLM lỗi thì bỏ qua
                    sentences = None
                    if log:
                        log(f"Dòng {item['line']}: LLM chấm câu lỗi, giữ nguyên: {exc}", "warn")
                if sentences and len(sentences) > 1:
                    pieces = split_item(item, min_piece_ms, margin_ms, join_flag, sentences)
                    if len(pieces) > 1:
                        llm_used += 1
        if len(pieces) > 1:
            split_count += 1
        out.extend(pieces)

    for index, piece in enumerate(out, 1):
        piece["line"] = index
    if log and split_count:
        log(f"Tách câu theo mốc từ: {split_count} dòng -> {len(out)} dòng"
            + (f" ({llm_used} dòng nhờ LLM chấm câu)" if llm_used else ""), "info")
    return out
