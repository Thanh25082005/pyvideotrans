"""Cắt câu bằng phân tích phổ (không cần host model VAD).

Loli 2.0 chỉ trả về text, không trả timestamp, nên phải tự chia audio thành các
đoạn có thời điểm bắt đầu/kết thúc rồi nhận dạng từng đoạn - đúng cách
videotrans/recognition/_base.py::cut_audio làm với các API không có timestamp.

Bản này thêm hai lớp để nhạc nền không bị nhận nhầm là giọng nói:

1. Trừ nền (spectral subtraction). Ước lượng "sàn" năng lượng theo từng dải tần
   trên cửa sổ trượt vài giây, rồi chỉ giữ phần vượt lên trên sàn đó. Nhạc nền
   đều đều sẽ chìm vào sàn, giọng nói nhô lên -> biên câu bám đúng lúc người nói
   mở/ngậm miệng thay vì bám theo lúc nhạc to/nhỏ.

2. Chấm điểm "giống giọng nói" cho từng cửa sổ 1,2s bằng các đặc trưng kinh điển
   để phân biệt speech/music (Scheirer & Slaney 1997):
     - mod4: năng lượng điều biến quanh 4Hz của đường bao - nhịp âm tiết của
       giọng người. Nhạc điều biến chậm hơn (nhịp phách 1-3Hz) hoặc rất đều.
     - dyn: biên độ dao động dB trong cửa sổ - giọng nói có hố sâu giữa các âm
       tiết, nhạc nền/nốt ngân thì phẳng.
     - spread: độ tản cao độ - giọng người quét vài quãng trong mỗi giây, nhạc
       thì bám thang âm nên cao độ gần như đứng yên.
     - harm_ratio: giọng nói xen kẽ hữu thanh/vô thanh nên độ hài dao động mạnh.
     - band: tỉ lệ năng lượng nằm trong dải 300-3400Hz của giọng nói.
   Cửa sổ nào điểm thấp bị coi là nhạc: đoạn ứng viên bị gọt hai đầu, bị cắt
   đôi nếu có khoảng nhạc chen dài hơn music_gap_ms, hoặc loại hẳn nếu không
   còn vùng nào ra hồn.

Lớp 2 chỉ *lọc bớt*, không tạo thêm đoạn, và có chốt an toàn: nếu nó định vứt
gần hết file mà vẫn có chỗ suýt đạt ngưỡng thì coi như đặc trưng không đáng tin
và giữ nguyên kết quả VAD. Ngược lại, cả file điểm bét (video thuần nhạc) thì
tin bộ lọc và trả rỗng để pipeline báo lỗi rõ ràng.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .ffmpeg import decode_pcm

SR = 16000
N_FFT = 512                       # 32ms - đủ phân giải để thấy hài của giọng
HOP_MS = 10
FRAME_MS = int(N_FFT * 1000 / SR)  # 32
HOP = SR * HOP_MS // 1000          # 160 mẫu

# Xử lý STFT theo lô để file dài không nuốt hết RAM
BLOCK_FRAMES = 3000               # ~30s mỗi lô

N_BANDS = 24
BAND_LO_HZ = 60.0
BAND_HI_HZ = 7600.0
# Dải tần của giọng nói - dùng cho cả gate năng lượng lẫn đặc trưng band
VOICE_LO_HZ = 280.0
VOICE_HI_HZ = 3800.0
# Cao độ giọng người: 70Hz (nam trầm) đến 400Hz (nữ/trẻ em cao)
F0_LO_HZ = 70.0
F0_HI_HZ = 400.0

FLOOR_BLOCK_MS = 3000             # cửa sổ ước lượng sàn nền
FLOOR_PERCENTILE = 12.0
OVER_SUBTRACT = 1.25              # trừ dư một chút cho chắc tay

SCORE_WIN_MS = 1200               # cửa sổ chấm điểm speech/music
SCORE_HOP_MS = 200
# Cửa sổ đạt điểm chỉ đánh dấu phần lõi quanh tâm -> biên bám sát giọng thật
MARK_HALF_MS = 300
# Vùng thoại ngắn hơn mức này thì coi như tiếng động lẻ, không đáng gửi ASR
MIN_SPEECH_RUN_MS = 400


# --------------------------------------------------------------------- đặc trưng
def _band_edges() -> np.ndarray:
    """Chỉ số bin FFT của N_BANDS dải log, đảm bảo mỗi dải có ít nhất 1 bin."""
    hz = np.geomspace(BAND_LO_HZ, BAND_HI_HZ, N_BANDS + 1)
    bins = np.round(hz * N_FFT / SR).astype(int)
    bins = np.clip(bins, 1, N_FFT // 2)
    for i in range(1, bins.size):
        if bins[i] <= bins[i - 1]:
            bins[i] = bins[i - 1] + 1
    return np.clip(bins, 1, N_FFT // 2 + 1)


_EDGES = _band_edges()
_BAND_CENTER_HZ = np.sqrt(
    (_EDGES[:-1] * SR / N_FFT) * (np.maximum(_EDGES[1:] - 1, _EDGES[:-1]) * SR / N_FFT))
_VOICE_BANDS = np.flatnonzero((_BAND_CENTER_HZ >= VOICE_LO_HZ) & (_BAND_CENTER_HZ <= VOICE_HI_HZ))
if _VOICE_BANDS.size == 0:  # phòng hờ nếu ai đó chỉnh hằng số
    _VOICE_BANDS = np.arange(N_BANDS)

_LAG_LO = int(SR / F0_HI_HZ)   # 40
_LAG_HI = int(SR / F0_LO_HZ)   # 228


class Features:
    """Các chuỗi đặc trưng theo frame (bước HOP_MS) của cả file."""

    __slots__ = ("band_pow", "db", "harm", "f0", "resid_db", "n")

    def __init__(self, band_pow: np.ndarray, db: np.ndarray, harm: np.ndarray, f0: np.ndarray):
        self.band_pow = band_pow      # (T, N_BANDS) công suất tuyến tính
        self.db = db                  # (T,) mức toàn dải, dB
        self.harm = harm              # (T,) độ nổi của đỉnh cepstrum
        self.f0 = f0                  # (T,) cao độ ước lượng, Hz
        self.resid_db = db            # (T,) mức sau khi trừ nền, gán sau
        self.n = int(db.size)


def _extract_features(samples: np.ndarray) -> Optional[Features]:
    if samples.size < N_FFT:
        return None
    count = 1 + (samples.size - N_FFT) // HOP
    window = np.hanning(N_FFT).astype(np.float32)
    base = np.arange(N_FFT)[None, :]

    band_pow = np.zeros((count, N_BANDS), dtype=np.float32)
    db = np.zeros(count, dtype=np.float32)
    harm = np.zeros(count, dtype=np.float32)
    f0 = np.zeros(count, dtype=np.float32)

    for start in range(0, count, BLOCK_FRAMES):
        stop = min(start + BLOCK_FRAMES, count)
        idx = base + HOP * np.arange(start, stop)[:, None]
        frames = samples[idx].astype(np.float32) * window

        spec = np.fft.rfft(frames, axis=1)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        power *= 1.0 / (N_FFT * N_FFT)

        trimmed = power[:, : _EDGES[-1]]
        band_pow[start:stop] = np.add.reduceat(trimmed, _EDGES[:-1], axis=1)
        db[start:stop] = 10.0 * np.log10(power.sum(axis=1) + 1e-12)

        # Cepstrum: đỉnh trong dải quefrency của giọng người = có cao độ rõ ràng.
        ceps = np.fft.irfft(np.log(power + 1e-12), axis=1)
        region = ceps[:, _LAG_LO:_LAG_HI]
        peak_at = np.argmax(region, axis=1)
        peak = region[np.arange(region.shape[0]), peak_at]
        baseline = np.mean(np.abs(region), axis=1) + 1e-9
        harm[start:stop] = (peak / baseline).astype(np.float32)
        f0[start:stop] = (SR / (_LAG_LO + peak_at)).astype(np.float32)

    return Features(band_pow, db, harm, f0)


def _smooth_floor(block_db: np.ndarray) -> np.ndarray:
    """Sàn nền: lấy min của 3 block liền kề (bỏ qua lúc có tiếng) rồi làm mượt."""
    if block_db.shape[0] < 3:
        return block_db
    pad = np.pad(block_db, ((1, 1), (0, 0)), mode="edge")
    low = np.minimum(np.minimum(pad[:-2], pad[1:-1]), pad[2:])
    pad = np.pad(low, ((1, 1), (0, 0)), mode="edge")
    return (pad[:-2] + pad[1:-1] + pad[2:]) / 3.0


def _apply_noise_floor(feat: Features) -> None:
    """Trừ nền theo từng dải tần -> resid_db chỉ còn phần nhô lên trên nhạc nền."""
    band_db = (10.0 * np.log10(feat.band_pow + 1e-12)).astype(np.float32)
    step = max(1, FLOOR_BLOCK_MS // HOP_MS)
    n_blocks = max(1, math.ceil(feat.n / step))
    pad_to = n_blocks * step
    if pad_to > feat.n:
        band_db = np.pad(band_db, ((0, pad_to - feat.n), (0, 0)), mode="edge")

    blocks = band_db.reshape(n_blocks, step, N_BANDS)
    floor_blk = _smooth_floor(np.percentile(blocks, FLOOR_PERCENTILE, axis=1)).astype(np.float32)
    del blocks, band_db

    if n_blocks == 1:
        floor_db = np.repeat(floor_blk, feat.n, axis=0)
    else:
        centers = np.arange(n_blocks) * step + step / 2.0
        grid = np.arange(feat.n, dtype=np.float64)
        floor_db = np.empty((feat.n, N_BANDS), dtype=np.float32)
        for b in range(N_BANDS):
            floor_db[:, b] = np.interp(grid, centers, floor_blk[:, b])

    resid = feat.band_pow[:, _VOICE_BANDS] - OVER_SUBTRACT * (10.0 ** (floor_db[:, _VOICE_BANDS] / 10.0))
    np.clip(resid, 0.0, None, out=resid)
    resid_db = 10.0 * np.log10(resid.sum(axis=1) + 1e-12)
    # Kẹp đáy để thống kê phân vị không bị -120dB kéo lệch
    ceiling = float(np.percentile(resid_db, 98)) if resid_db.size else 0.0
    feat.resid_db = np.maximum(resid_db, ceiling - 60.0).astype(np.float32)


# ------------------------------------------------------- chấm điểm speech/music
def _mod_ratio(env: np.ndarray) -> float:
    """Tỉ lệ năng lượng điều biến 2,5-7,5Hz - nhịp âm tiết của giọng người."""
    if env.size < 60:  # dưới 0,6s thì phân giải tần số không đủ để kết luận
        return -1.0
    x = (env - env.mean()) * np.hanning(env.size)
    nfft = 1 << max(8, int(math.ceil(math.log2(env.size))) + 1)
    spec = np.abs(np.fft.rfft(x, n=nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft, d=HOP_MS / 1000.0)
    syllabic = spec[(freqs >= 2.5) & (freqs <= 7.5)].sum()
    total = spec[(freqs >= 0.5) & (freqs <= 16.0)].sum()
    return float(syllabic / (total + 1e-9))


def _window_features(feat: Features, lo: int, hi: int) -> Dict[str, float]:
    """Đặc trưng phân biệt giọng nói / nhạc trên một cửa sổ."""
    env = feat.resid_db[lo:hi]
    harm = feat.harm[lo:hi]
    f0 = feat.f0[lo:hi]
    band = feat.band_pow[lo:hi]

    # dyn: giọng nói có hố sâu giữa các âm tiết, nhạc nền/nốt ngân thì phẳng lì.
    dyn = float(np.percentile(env, 95) - np.percentile(env, 15)) if env.size >= 8 else 0.0

    # spread: cao độ giọng người quét vài quãng trong một giây; nhạc bám thang âm.
    if f0.size >= 16:
        spread = float(np.std(12.0 * np.log2(np.maximum(f0, 1.0))))
    else:
        spread = -1.0

    # harm_ratio: giọng nói xen kẽ hữu thanh (hài rõ) và vô thanh (hài tắt) nên
    # độ nổi của đỉnh cepstrum dao động mạnh; nhạc cụ thì đều đều.
    if harm.size >= 16:
        harm_ratio = float(np.percentile(harm, 90) / (np.percentile(harm, 50) + 1e-6))
    else:
        harm_ratio = -1.0

    voice_pow = band[:, _VOICE_BANDS].sum(axis=1)
    all_pow = band.sum(axis=1) + 1e-12
    band_ratio = float(np.mean(voice_pow / all_pow)) if band.shape[0] else 0.0

    return {"mod4": _mod_ratio(env), "dyn": dyn, "spread": spread,
            "harm_ratio": harm_ratio, "band": band_ratio}


def _clip01(value: float) -> float:
    return float(min(1.0, max(0.0, value)))


def _speech_score(f: Dict[str, float]) -> float:
    """Gộp đặc trưng thành một điểm 0..1; đặc trưng không đo được thì bỏ phiếu.

    Ngưỡng lấy từ file kiểm thử tổng hợp: nhạc thuần rơi vào 0,05-0,20 còn thoại
    (kể cả thoại đè trên nhạc nền) nằm ở 0,75-0,95.
    """
    parts: List[Tuple[float, float]] = []  # (điểm, trọng số)
    parts.append((_clip01((f["dyn"] - 4.0) / 9.0), 0.30))
    if f["spread"] >= 0:
        parts.append((_clip01((f["spread"] - 2.2) / 2.6), 0.26))
    if f["mod4"] >= 0:
        parts.append((_clip01((f["mod4"] - 0.22) / 0.26), 0.24))
    if f["harm_ratio"] >= 0:
        parts.append((_clip01((f["harm_ratio"] - 1.25) / 0.45), 0.12))
    parts.append((_clip01((f["band"] - 0.30) / 0.34), 0.08))
    weight = sum(w for _, w in parts)
    return float(sum(s * w for s, w in parts) / weight) if weight else 1.0


def _score_windows(feat: Features, start_ms: int, end_ms: int) -> List[Tuple[int, int, float]]:
    """Chấm điểm từng cửa sổ trượt trong đoạn -> [(start_ms, end_ms, score)]."""
    lo = max(0, start_ms // HOP_MS)
    hi = min(feat.n, max(lo + 1, end_ms // HOP_MS))
    win = SCORE_WIN_MS // HOP_MS
    hop = SCORE_HOP_MS // HOP_MS
    if hi - lo <= win:
        return [(lo * HOP_MS, hi * HOP_MS, _speech_score(_window_features(feat, lo, hi)))]
    out: List[Tuple[int, int, float]] = []
    positions = list(range(lo, hi - win + 1, hop))
    if positions[-1] != hi - win:
        positions.append(hi - win)
    for pos in positions:
        out.append((pos * HOP_MS, (pos + win) * HOP_MS,
                    _speech_score(_window_features(feat, pos, pos + win))))
    return out


def _speech_spans(windows: Sequence[Tuple[int, int, float]], seg_start: int, seg_end: int,
                  threshold: float, gap_ms: int, min_run_ms: int) -> List[List[int]]:
    """Đổi điểm từng cửa sổ thành các vùng có thoại bên trong một đoạn.

    Mỗi cửa sổ đạt điểm chỉ đánh dấu phần lõi quanh tâm nó (MARK_HALF_MS) để
    biên bám sát chỗ giọng thật sự bắt đầu, thay vì phình ra cả cửa sổ 1,2s.
    Cửa sổ đầu/cuối thì kéo thẳng ra mép đoạn để không cụt chữ.
    """
    marks: List[List[int]] = []
    for i, (w_start, w_end, score) in enumerate(windows):
        if score < threshold:
            continue
        center = (w_start + w_end) // 2
        lo = center - MARK_HALF_MS
        hi = center + MARK_HALF_MS
        if i == 0:
            lo = min(lo, seg_start)
        if i == len(windows) - 1:
            hi = max(hi, seg_end)
        marks.append([max(seg_start, lo), min(seg_end, hi)])
    if not marks:
        return []
    # Nối các mảnh cách nhau ít hơn gap_ms: khoảng nhạc chen giữa hai câu mà quá
    # ngắn thì cứ để nguyên trong một đoạn, cắt vụn chỉ tổ hại ngữ cảnh cho ASR.
    merged: List[List[int]] = [marks[0]]
    for span in marks[1:]:
        if span[0] - merged[-1][1] <= gap_ms:
            merged[-1][1] = max(merged[-1][1], span[1])
        else:
            merged.append(span)
    if len(merged) == 1:
        # Cả đoạn chỉ ra một vùng -> đã có cửa sổ đạt ngưỡng thì phải giữ, dù ngắn.
        # Câu cảm thán ("Aha!", "Oh!") chỉ dài 250-350ms; bước gộp đoạn ngắn ở
        # cuối pipeline mới là chỗ xử lý chúng, không phải ở đây.
        return merged
    # Chỉ vứt mảnh vụn sinh ra do cắt đôi, không vứt cả đoạn gốc.
    kept = [s for s in merged if s[1] - s[0] >= min_run_ms]
    return kept or [max(merged, key=lambda s: s[1] - s[0])]


def _filter_music(segments: Sequence[Sequence[int]], feat: Features, threshold: float,
                  gap_ms: int, min_run_ms: int) -> Tuple[List[List[int]], List[Dict]]:
    """Bỏ vùng chỉ có nhạc: gọt hai đầu, cắt đôi khi nhạc chen giữa, loại đoạn câm."""
    kept: List[List[int]] = []
    report: List[Dict] = []
    for start, end in segments:
        start, end = int(start), int(end)
        windows = _score_windows(feat, start, end)
        spans = _speech_spans(windows, start, end, threshold, gap_ms, min_run_ms)
        info = {
            "start": start, "end": end,
            "score": round(max((w[2] for w in windows), default=0.0), 3),
            "windows": len(windows),
            "speech_windows": sum(1 for w in windows if w[2] >= threshold),
            "pieces": [list(s) for s in spans],
        }
        if not spans:
            info["action"] = "drop"
        elif len(spans) > 1:
            info["action"] = "split"
        elif spans[0][0] > start or spans[0][1] < end:
            info["action"] = "trim"
        else:
            info["action"] = "keep"
        report.append(info)
        kept.extend(spans)
    return kept, report


# ------------------------------------------------------------------ ghép/cắt đoạn
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


def _energy_gate(db: np.ndarray) -> np.ndarray:
    """Gate hai ngưỡng (Schmitt trigger) trên chuỗi mức dB."""
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
        active = value > leave if active else value > enter
        voiced[i] = active
    return voiced


# ------------------------------------------------------------------------ API
def analyze(wav_16k: str, min_speech_ms: int = 1200, max_speech_ms: int = 18000,
            min_silence_ms: int = 400, pad_ms: int = 120,
            music_filter: bool = True, speech_score_min: float = 0.45,
            music_gap_ms: int = 700, subtract_floor: bool = True) -> Dict:
    """Chạy VAD và trả về cả kết quả lẫn nhật ký để soi khi chỉnh tham số.

    music_gap_ms: khoảng nhạc phải dài hơn chừng này thì mới cắt đôi đoạn; nhạc
    chen ngắn hơn cứ để nguyên, cắt vụn chỉ làm ASR mất ngữ cảnh.

    Trả về dict: segments, dropped/trimmed/split (số đoạn ứng viên bị xử lý),
    report (chi tiết từng đoạn), fallback (True nếu bộ lọc nhạc bị vô hiệu vì
    nó định vứt gần hết file).
    """
    samples = decode_pcm(wav_16k, sample_rate=SR, channels=1)
    empty = {"segments": [], "report": [], "dropped": 0, "trimmed": 0, "split": 0,
             "fallback": False}
    if samples.size == 0:
        return empty
    total_ms = int(samples.size * 1000 / SR)

    feat = _extract_features(samples)
    if feat is None or feat.n == 0:
        return {**empty, "segments": [(0, total_ms)]}
    if subtract_floor:
        _apply_noise_floor(feat)

    gate_db = feat.resid_db if subtract_floor else feat.db
    segments = _close_gaps(_mask_to_segments(_energy_gate(gate_db)), min_silence_ms)
    segments = [s for s in segments if s[1] - s[0] >= 250]
    if not segments:
        return {**empty, "segments": [(0, total_ms)]}

    report: List[Dict] = []
    fallback = False
    if music_filter:
        candidate = _close_gaps([list(s) for s in segments], min_silence_ms)
        kept, report = _filter_music(candidate, feat, speech_score_min,
                                     music_gap_ms, MIN_SPEECH_RUN_MS)
        voiced_before = sum(e - s for s, e in segments)
        voiced_after = sum(e - s for s, e in kept)
        best = max((r["score"] for r in report), default=0.0)
        # Chốt an toàn: bộ lọc chỉ được phép gọt bớt, không được xoá sổ cả file.
        # Nhưng chỉ chữa cháy khi *có* chỗ suýt đạt ngưỡng - tức đặc trưng vẫn
        # thấy dáng dấp giọng nói, chỉ là ngưỡng đặt hơi cao. Nếu cả file đều
        # điểm bét (video thuần nhạc) thì tin bộ lọc và trả về rỗng, để pipeline
        # báo lỗi rõ ràng thay vì nạp 30 phút nhạc vào ASR.
        if (not kept or voiced_after < 0.25 * voiced_before) and best >= speech_score_min * 0.6:
            fallback = True
            for info in report:
                info["action"] = "keep(fallback)"
                info["pieces"] = [[info["start"], info["end"]]]
        else:
            segments = kept

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
        expanded.extend(_split_long(seg, gate_db, max_speech_ms, min_speech_ms))

    final = _merge_short(expanded, min_speech_ms, max_speech_ms)
    # Loli 2.0 yêu cầu audio dài tối thiểu 0,2s -> bỏ các mẩu quá ngắn
    result = [(int(s), int(e)) for s, e in final if e - s >= 300]
    return {
        "segments": result,
        "report": report,
        "dropped": sum(1 for r in report if r["action"] == "drop"),
        "trimmed": sum(1 for r in report if r["action"] == "trim"),
        "split": sum(1 for r in report if r["action"] == "split"),
        "fallback": fallback,
    }


def detect_segments(wav_16k: str, min_speech_ms: int = 1200, max_speech_ms: int = 18000,
                    min_silence_ms: int = 400, pad_ms: int = 120,
                    music_filter: bool = True, speech_score_min: float = 0.45,
                    music_gap_ms: int = 700, subtract_floor: bool = True) -> List[Tuple[int, int]]:
    """Trả về danh sách (start_ms, end_ms) các đoạn có tiếng nói."""
    return analyze(wav_16k, min_speech_ms=min_speech_ms, max_speech_ms=max_speech_ms,
                   min_silence_ms=min_silence_ms, pad_ms=pad_ms, music_filter=music_filter,
                   speech_score_min=speech_score_min, music_gap_ms=music_gap_ms,
                   subtract_floor=subtract_floor)["segments"]


def _ts(ms: int) -> str:
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def main() -> None:
    """Soi kết quả VAD trên một file để chỉnh tham số:

        .venv/bin/python -m core.vad video.mp4 [--score 0.38] [--no-music-filter]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Soi VAD + bộ lọc nhạc nền")
    parser.add_argument("path")
    parser.add_argument("--score", type=float, default=0.45)
    parser.add_argument("--no-music-filter", action="store_true")
    parser.add_argument("--no-subtract", action="store_true")
    parser.add_argument("--gap", type=int, default=700, help="ms nhạc tối thiểu để cắt đôi đoạn")
    args = parser.parse_args()

    out = analyze(args.path, music_filter=not args.no_music_filter,
                  speech_score_min=args.score, music_gap_ms=args.gap,
                  subtract_floor=not args.no_subtract)
    for info in out["report"]:
        mark = {"drop": "x", "trim": "~", "split": "/", "keep": " "}.get(info["action"], "!")
        pieces = info.get("pieces") or []
        extra = ""
        if info["action"] in ("trim", "split"):
            extra = " -> " + ", ".join(f"{_ts(a)}-{_ts(b)}" for a, b in pieces)
        print(f" {mark} {_ts(info['start'])}-{_ts(info['end'])} "
              f"score={info['score']:.2f} win={info['speech_windows']}/{info['windows']}{extra}")
    print(f"\n{len(out['segments'])} đoạn gửi ASR | loại {out['dropped']} | gọt {out['trimmed']}"
          f" | cắt đôi {out['split']}"
          f"{' | FALLBACK (bộ lọc bị vô hiệu)' if out['fallback'] else ''}")
    total = sum(e - s for s, e in out["segments"]) / 1000.0
    print(f"tổng thoại {total:.1f}s")
    for start, end in out["segments"]:
        print(f"   {_ts(start)} -> {_ts(end)}  ({(end - start) / 1000:.1f}s)")


if __name__ == "__main__":
    main()
