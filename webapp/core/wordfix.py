"""Nắn lại những từ bị forced aligner kéo dãn qua khoảng lặng.

Aligner buộc phải phủ kín clip bằng các từ trong transcript. Gặp đoạn đầu clip là
nhạc nền hoặc tiếng động, nó thường dán từ đầu tiên lên toàn bộ đoạn đó:

    Who   rel 0.56 - 6.16   (5.6 giây cho một từ 3 chữ cái!)
    are   rel 6.24 - 6.48
    you   rel 6.48 - 6.80

Mốc bắt đầu của câu bị lấy theo `Who` nên phụ đề và tiếng lồng xuất phát sớm hơn
giọng thật gần 6 giây. Thực tế cả ba từ nằm chụm ở cuối clip.

Cách xử lý: lấy **trung vị giây/ký tự** của các từ đã căn được - trung vị nên vài từ
dị thường không kéo lệch được nó - rồi so từng từ với độ dài đáng lẽ phải có. Từ nào
dài gấp nhiều lần thì co lại về đúng phía có hàng xóm đứng sát:

    Who có hàng xóm sát ở cuối (0.08s tới `are`) -> neo vào cuối
    -> rel 5.84 - 6.16, câu xuất phát từ 5.84 thay vì 0.56

Chỉ sửa mốc thời gian, không đụng tới chữ.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)
# Giây trên mỗi ký tự khi chưa có mẫu nào để tính trung vị
DEFAULT_RATE = 0.09
# Sàn/trần cho độ dài ước lượng của một từ sau khi co lại
MIN_WORD_S = 0.08
MAX_WORD_S = 1.20


def _chars(text: str) -> int:
    return len(STRIP_RE.sub("", text or ""))


def median(values: Sequence[float]) -> float:
    data = sorted(v for v in values if v > 0)
    if not data:
        return 0.0
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def _rates_of(words: Sequence[Dict]) -> List[float]:
    out = []
    for word in words:
        chars = _chars(str(word.get("word", "")))
        duration = float(word.get("end", 0)) - float(word.get("start", 0))
        if chars > 0 and duration > 0:
            out.append(duration / chars)
    return out


class RateTracker:
    """Gom giây/ký tự của mọi từ đã căn trong video để lấy trung vị chung.

    Dòng đầu tiên chưa có mẫu thì dùng ngay trung vị của chính dòng đó; càng về sau
    mẫu càng dày nên ngưỡng càng ổn định.
    """

    def __init__(self, default: float = DEFAULT_RATE, cap: int = 5000):
        self.default = default
        self.cap = cap
        self.samples: List[float] = []

    def observe(self, words: Sequence[Dict]) -> None:
        if len(self.samples) < self.cap:
            self.samples.extend(_rates_of(words))

    def rate(self, words: Sequence[Dict]) -> float:
        pooled = median(self.samples + _rates_of(words))
        return pooled if pooled > 0 else self.default


def fix_stretched(words: List[Dict], rate: float, factor: float = 4.0,
                  min_stretch_s: float = 0.8, clip_s: Optional[float] = None
                  ) -> Tuple[List[Dict], List[Dict]]:
    """Co những từ bị kéo dãn về sát cụm từ bên cạnh.

    Trả về (danh sách từ đã sửa, ghi chú từng chỗ sửa). Danh sách gốc không bị đụng.
    """
    fixed = [dict(word) for word in words]
    notes: List[Dict] = []
    if len(fixed) < 2 or rate <= 0:
        return fixed, notes

    for index, word in enumerate(fixed):
        start = float(word.get("start", 0))
        end = float(word.get("end", 0))
        duration = end - start
        chars = _chars(str(word.get("word", "")))
        if chars <= 0 or duration <= 0:
            continue
        expected = min(MAX_WORD_S, max(MIN_WORD_S, chars * rate))
        if duration < min_stretch_s or duration < expected * factor:
            continue

        prev_end = float(fixed[index - 1]["end"]) if index > 0 else None
        next_start = float(fixed[index + 1]["start"]) if index + 1 < len(fixed) else None
        gap_before = (start - prev_end) if prev_end is not None else float("inf")
        gap_after = (next_start - end) if next_start is not None else float("inf")

        if gap_after <= gap_before:
            # Cụm từ nằm ở phía sau -> giọng thật ở cuối khoảng bị kéo
            new_end = end
            new_start = max(prev_end if prev_end is not None else 0.0, end - expected)
        else:
            # Cụm từ nằm ở phía trước -> giọng thật ở đầu khoảng bị kéo
            new_start = start
            new_end = start + expected
            if next_start is not None:
                new_end = min(new_end, next_start)
        if clip_s is not None:
            new_end = min(new_end, clip_s)
            new_start = min(new_start, new_end)
        if new_end - new_start <= 0:
            continue

        notes.append({
            "word": str(word.get("word", "")),
            "index": index,
            "from": [round(start, 3), round(end, 3)],
            "to": [round(new_start, 3), round(new_end, 3)],
            "expected": round(expected, 3),
            "anchor": "cuối" if gap_after <= gap_before else "đầu",
        })
        word["start"] = round(new_start, 3)
        word["end"] = round(new_end, 3)
    return fixed, notes


def lone_edges(words: Sequence[Dict], factor: float = 5.0, min_gap_s: float = 2.0) -> List[int]:
    """Đánh dấu từ đầu/cuối bị tách rời hẳn khỏi cụm còn lại.

    Chỉ báo, không sửa. Từ đứng lẻ trước cụm có thể là aligner khớp nhầm vào tiếng
    động, mà cũng có thể là người nói thật rồi ngừng một nhịp - hai trường hợp này
    không phân biệt được chỉ bằng mốc thời gian, nên đây để mắt người quyết định.
    """
    if len(words) < 3:
        return []
    gaps = [float(words[i + 1]["start"]) - float(words[i]["end"]) for i in range(len(words) - 1)]
    typical = median(gaps)
    if typical <= 0:
        typical = 0.05
    threshold = max(min_gap_s, factor * typical)
    flagged = []
    if gaps[0] > threshold:
        flagged.append(0)
    if gaps[-1] > threshold:
        flagged.append(len(words) - 1)
    return flagged


def describe(note: Dict) -> str:
    """Một dòng mô tả gọn cho log."""
    return (f"'{note['word']}' {note['from'][0]:.2f}-{note['from'][1]:.2f}s"
            f" -> {note['to'][0]:.2f}-{note['to'][1]:.2f}s"
            f" (đáng lẽ ~{note['expected']:.2f}s, neo vào {note['anchor']})")
