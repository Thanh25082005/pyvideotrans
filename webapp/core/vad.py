"""Cắt câu bằng năng lượng tín hiệu (không cần host model VAD).

Loli 2.0 chỉ trả về text, không trả timestamp, nên phải tự chia audio thành các
đoạn có thời điểm bắt đầu/kết thúc rồi nhận dạng từng đoạn - đúng cách
videotrans/recognition/_base.py::cut_audio làm với các API không có timestamp.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

from .ffmpeg import decode_pcm

SR = 16000
FRAME_MS = 20
HOP_MS = 10


def _frame_db(samples: np.ndarray) -> np.ndarray:
    frame_len = int(SR * FRAME_MS / 1000)
    hop = int(SR * HOP_MS / 1000)
    if samples.size < frame_len:
        return np.zeros(0, dtype=np.float32)
    count = 1 + (samples.size - frame_len) // hop
    idx = np.arange(frame_len)[None, :] + hop * np.arange(count)[:, None]
    frames = samples[idx].astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20.0 * np.log10(rms + 1e-6)


def _mask_to_segments(mask: np.ndarray) -> List[List[int]]:
    """Đổi mask theo frame thành danh sách [start_ms, end_ms]."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [[int(s * HOP_MS), int(e * HOP_MS + FRAME_MS)] for s, e in zip(edges[::2], edges[1::2])]


def _close_gaps(segments: List[List[int]], min_silence_ms: int) -> List[List[int]]:
    merged: List[List[int]] = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] < min_silence_ms:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))
    return merged


def _split_long(seg: List[int], db: np.ndarray, max_ms: int, min_ms: int) -> List[List[int]]:
    """Chia đoạn quá dài tại điểm năng lượng thấp nhất gần cuối cửa sổ cho phép."""
    out: List[List[int]] = []
    queue = [list(seg)]
    while queue:
        start, end = queue.pop(0)
        if end - start <= max_ms:
            out.append([start, end])
            continue
        lo = start + int(max_ms * 0.55)
        hi = min(end - min_ms // 2, start + max_ms)
        if hi <= lo:
            cut = start + max_ms
        else:
            f_lo, f_hi = lo // HOP_MS, min(hi // HOP_MS, db.size - 1)
            if f_hi <= f_lo:
                cut = start + max_ms
            else:
                cut = int((f_lo + int(np.argmin(db[f_lo:f_hi]))) * HOP_MS)
        cut = max(start + min_ms // 2, min(cut, end - min_ms // 2))
        out.append([start, cut])
        queue.insert(0, [cut, end])
    return out


def _merge_short(segments: List[List[int]], min_ms: int, max_ms: int,
                 max_gap_ms: int = 2000) -> List[List[int]]:
    """Gộp đoạn quá ngắn vào hàng xóm gần nhất (theo _base.cut_audio của repo)."""
    if not segments:
        return []
    result = [list(segments[0])]
    for seg in segments[1:]:
        prev = result[-1]
        prev_short = (prev[1] - prev[0]) < min_ms
        # chỉ gộp khi hai đoạn thực sự gần nhau, tránh nuốt cả khoảng lặng dài
        can_merge = (seg[1] - prev[0]) <= max_ms and (seg[0] - prev[1]) <= max_gap_ms
        if prev_short and can_merge:
            prev[1] = seg[1]
        else:
            result.append(list(seg))
    # đoạn cuối quá ngắn thì nhập vào đoạn trước
    if len(result) > 1 and (result[-1][1] - result[-1][0]) < min_ms // 2:
        if result[-1][1] - result[-2][0] <= max_ms * 1.5:
            result[-2][1] = result[-1][1]
            result.pop()
    return result


def detect_segments(wav_16k: str, min_speech_ms: int = 1200, max_speech_ms: int = 18000,
                    min_silence_ms: int = 400, pad_ms: int = 120) -> List[Tuple[int, int]]:
    """Trả về danh sách (start_ms, end_ms) các đoạn có tiếng nói."""
    samples = decode_pcm(wav_16k, sample_rate=SR, channels=1)
    if samples.size == 0:
        return []
    total_ms = int(samples.size * 1000 / SR)
    db = _frame_db(samples)
    if db.size == 0:
        return [(0, total_ms)]

    noise = float(np.percentile(db, 20))
    peak = float(np.percentile(db, 95))
    dynamic = peak - noise
    if dynamic < 6:
        # Tín hiệu gần như đều (nhạc nền lớn / nói liên tục) -> ngưỡng sát nền
        enter = noise + max(1.5, dynamic * 0.4)
    else:
        enter = max(noise + 6.0, peak - 22.0)
    leave = enter - 3.0

    voiced = np.zeros(db.size, dtype=bool)
    active = False
    for i, value in enumerate(db):
        if active:
            active = value > leave
        else:
            active = value > enter
        voiced[i] = active

    # Nối các mảnh liền nhau trước rồi mới lọc theo độ dài: giọng nói luôn bị
    # ngắt quãng bởi phụ âm tắc, lọc trước sẽ xoá mất nửa câu.
    segments = _close_gaps(_mask_to_segments(voiced), min_silence_ms)
    segments = [s for s in segments if s[1] - s[0] >= 250]
    if not segments:
        return [(0, total_ms)]

    padded: List[List[int]] = []
    for i, (start, end) in enumerate(segments):
        prev_end = padded[-1][1] if padded else 0
        start = max(prev_end, start - pad_ms, 0)
        next_start = segments[i + 1][0] if i + 1 < len(segments) else total_ms
        end = min(end + pad_ms, next_start, total_ms)
        if end > start:
            padded.append([start, end])

    expanded: List[List[int]] = []
    for seg in padded:
        expanded.extend(_split_long(seg, db, max_speech_ms, min_speech_ms))

    final = _merge_short(expanded, min_speech_ms, max_speech_ms)
    # Loli 2.0 yêu cầu audio dài tối thiểu 0,2s -> bỏ các mẩu quá ngắn
    return [(int(s), int(e)) for s, e in final if e - s >= 300]
