"""Pipeline dịch video: chuẩn bị -> nhận dạng -> dịch -> lồng tiếng -> căn chỉnh -> ghép.

Bám theo luồng của pyVideoTrans (videotrans/task/_stage_*.py) nhưng thay ba khối
model bằng API: Loli 2.0 (ASR), OpenAI (dịch), Loly 3.5 (TTS). Không host model nào,
chỉ dùng ffmpeg + một bộ VAD năng lượng viết bằng numpy để cắt câu.

Khác biệt so với repo gốc ở khâu đồng bộ:
- Tốc độ đọc được ước lượng và đặt ngay trong request TTS (`speed`), thay vì chỉ
  kéo giãn bằng atempo sau khi đã sinh xong - giọng tự nhiên hơn nhiều.
- Con trỏ thời gian tự bắt lại nhịp: mỗi câu luôn cố phát đúng mốc gốc của nó,
  câu tràn chỉ đẩy trễ những câu sát ngay sau, gặp khoảng lặng là hết lệch.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import editor, ffmpeg, resegment, separate, srt, vad, wordfix
from .langs import CJK_LANGS, english_name
from .forced_aligner import ForcedAlignerError, QwenForcedAlignerClient, supported_language
from .stt_loli import LoliSTT, SttError
from .translate_openai import OpenAITranslator, TranslateError, estimate_cost
from .tts_loly import LolyTTS, TtsError

SR = ffmpeg.SAMPLE_RATE
# Loly 3.5 chỉ nhận speed trong khoảng 0,5-1,5
TTS_SPEED_MIN, TTS_SPEED_MAX = 0.5, 1.5


class PipelineError(RuntimeError):
    pass


def _ts(ms: float) -> str:
    """Định dạng mốc thời gian tuyệt đối cho log: 00:01:02.340"""
    ms = max(0, int(round(ms)))
    h, rest = divmod(ms, 3600000)
    m, rest = divmod(rest, 60000)
    sec, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}.{milli:03d}"


def _silence(ms: int) -> np.ndarray:
    return np.zeros(max(0, int(ms * SR / 1000)), dtype=np.int16)


def _duration_ms(samples: np.ndarray) -> int:
    return int(samples.size * 1000 / SR)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def _trim_silence(samples: np.ndarray, floor_db: float = -45.0, margin_ms: int = 30) -> np.ndarray:
    """Cắt khoảng lặng đầu/cuối của đoạn TTS để lồng tiếng bám đúng mốc thời gian."""
    if samples.size == 0:
        return samples
    win = max(1, int(SR * 0.01))
    trimmed = samples[: samples.size - samples.size % win]
    if trimmed.size == 0:
        return samples
    frames = trimmed.reshape(-1, win).astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1)) / 32768.0
    db = 20 * np.log10(rms + 1e-9)
    loud = np.flatnonzero(db > floor_db)
    if loud.size == 0:
        return samples
    margin = int(margin_ms * SR / 1000)
    start = max(0, loud[0] * win - margin)
    end = min(samples.size, (loud[-1] + 1) * win + margin)
    return samples[start:end]


class RateEstimator:
    """Ước lượng tốc độ đọc thực tế của voice: số ký tự đọc được trong 1 giây ở speed=1.

    Nhờ vậy các câu sau biết trước mình sẽ dài bao nhiêu và đặt `speed` hợp lý ngay
    từ request đầu tiên, thay vì phải sinh xong rồi mới sửa.
    """

    def __init__(self, weight: float = 0.3):
        self._lock = threading.Lock()
        self._cps: Optional[float] = None
        self._weight = weight
        self.samples = 0

    @property
    def cps(self) -> Optional[float]:
        return self._cps

    def update(self, chars: int, duration_ms: int, speed: float) -> None:
        if chars < 4 or duration_ms < 200:
            return
        # dur = chars / (cps * speed)  ->  cps = chars / (dur * speed)
        observed = chars / (duration_ms / 1000.0 * speed)
        with self._lock:
            self._cps = observed if self._cps is None else (
                self._cps * (1 - self._weight) + observed * self._weight)
            self.samples += 1

    def predict_ms(self, chars: int, speed: float) -> Optional[int]:
        with self._lock:
            cps = self._cps
        if not cps or cps <= 0:
            return None
        return int(chars / cps / max(speed, 0.1) * 1000)


def _clamp_volume(value, default: float) -> float:
    """Âm lượng người dùng gửi lên; sai kiểu hoặc trống thì lấy mặc định."""
    if value is None or value == "":
        return float(default)
    try:
        return max(0.0, min(float(value), 4.0))
    except (TypeError, ValueError):
        return float(default)


class Pipeline:
    def __init__(self, job, config: Dict):
        self.job = job
        self.config = config
        self.params = job.params
        self.cache = Path(job.dir) / "cache"
        self.output = Path(job.dir) / "output"
        pipe_cfg = config.get("pipeline", {})
        self.min_speech_ms = int(pipe_cfg.get("min_speech_ms", 1200))
        self.max_speech_ms = int(pipe_cfg.get("max_speech_ms", 18000))
        self.min_silence_ms = int(pipe_cfg.get("min_silence_ms", 400))
        # Phân biệt nhạc nền / giọng nói khi cắt câu (xem core/vad.py)
        self.subtract_music_floor = bool(pipe_cfg.get("subtract_music_floor", True))
        self.music_filter = bool(pipe_cfg.get("music_filter", True))
        self.speech_score_min = float(pipe_cfg.get("speech_score_min", 0.45))
        self.music_gap_ms = int(pipe_cfg.get("music_gap_ms", 700))
        sep_cfg = config.get("separate", {})
        self.separate_enabled = bool(sep_cfg.get("enabled", True))
        self.separate_url = sep_cfg.get("base_url", "http://127.0.0.1:8200")
        self.separate_timeout = float(sep_cfg.get("timeout", 1800))
        self.accompaniment_volume = float(sep_cfg.get("accompaniment_volume", 0.9))
        # Âm lượng do người dùng đặt cho từng job; không đặt thì lấy mặc định cấu hình.
        # background_volume áp cho nền (stem nhạc đã tách hoặc audio gốc),
        # dubbed_volume áp cho track lồng tiếng.
        self.dubbed_volume = _clamp_volume(self.params.get("dubbed_volume"), 1.0)
        self.background_volume = self.params.get("background_volume")
        # Nguồn tiếng nền: auto (ưu tiên stem nhạc đã tách) | original (đúng audio
        # gốc, không qua Demucs) | accompaniment (ép dùng stem)
        self.background_source = str(self.params.get("background_source") or "auto").lower()
        # Âm lượng giọng nhân vật gốc, tách riêng khỏi nhạc nền. 0 = tắt hẳn (mặc
        # định, giống lồng tiếng thường); >0 = giọng gốc phát cùng giọng TTS.
        self.original_voice_volume = _clamp_volume(self.params.get("original_voice_volume"), 0.0)
        # amix normalize=0 làm âm lượng đúng nghĩa nhưng có thể vượt ngưỡng;
        # limiter chỉ động vào đỉnh sắp clip. Tắt = nền đúng từng mẫu, tự chịu clip.
        self.mix_limiter = bool(sep_cfg.get("mix_limiter", True))
        self.accompaniment: Optional[Path] = None
        self.vocals: Optional[Path] = None
        self.max_audio_speed = float(self.params.get("max_audio_speed")
                                     or pipe_cfg.get("max_audio_speed", 1.6))
        self.stt_workers = max(1, int(pipe_cfg.get("stt_concurrency", 3)))
        self.tts_workers = max(1, int(pipe_cfg.get("tts_concurrency", 3)))
        # Sai lệch nhỏ hơn ngưỡng này thì bỏ qua, ép tốc độ chỉ làm giọng xấu đi
        self.fit_tolerance_ms = int(pipe_cfg.get("fit_tolerance_ms", 200))
        # Chừa một nhịp thở giữa hai câu liền nhau
        self.gap_reserve_ms = int(pipe_cfg.get("gap_reserve_ms", 120))
        # Trần tuyệt đối: câu lồng tiếng không được kéo dài quá lúc giọng gốc dứt
        # thêm chừng này. Khoảng lặng phía sau không phải chỗ để mượn.
        self.max_overrun_ms = int(pipe_cfg.get("max_overrun_ms", 500))
        # Khi speed native của TTS đã kịch mà vẫn tràn, atempo được phép ép tới đây
        self.hard_max_audio_speed = float(pipe_cfg.get("hard_max_audio_speed", 2.5))
        self.binary_iterations = max(1, min(int(pipe_cfg.get("binary_iterations", 4)), 8))
        self.rate = RateEstimator()
        align_cfg = config.get("aligner", {})
        # Nhật ký chi tiết để soi lỗi chèn lệch: từng từ + từng câu được đặt ở đâu
        self.align_debug = bool(align_cfg.get("debug_log", True))
        self.align_debug_ui = bool(align_cfg.get("log_words_to_ui", False))
        self.overshoot_warn_ms = int(align_cfg.get("overshoot_warn_ms", 150))
        # Nắn từ bị aligner kéo dãn qua khoảng lặng, so với trung vị giây/ký tự
        self.fix_stretched = bool(align_cfg.get("fix_stretched_words", True))
        self.stretch_factor = float(align_cfg.get("stretch_factor", 4.0))
        self.stretch_min_ms = int(align_cfg.get("stretch_min_ms", 800))
        self._rates = wordfix.RateTracker()
        self.align_log: Optional[Path] = None
        self.word_timestamps: Optional[Path] = None
        self.word_report: Dict = {}
        self.word_segments: Dict[int, Dict] = {}
        self._translator_cache: Optional[OpenAITranslator] = None
        self.token_usage: Dict = {}
        self.token_cost: Dict = {}
        self._tokens_reported: Tuple[int, int] = (-1, -1)

    # ------------------------------------------------------------------ utils
    def log(self, message: str, level: str = "info") -> None:
        self.job.log(message, level)

    def stage(self, name: str, progress: int) -> None:
        self.job.set_stage(name, progress)

    def _debug(self, lines: List[str]) -> None:
        """Ghi thêm vào output/align-debug.log. Log dài nên không đẩy hết lên UI."""
        if not self.align_debug or not lines:
            return
        if self.align_log is None:
            self.output.mkdir(parents=True, exist_ok=True)
            self.align_log = self.output / "align-debug.log"
            self.align_log.write_text(
                f"# Nhật ký căn chỉnh | job {self.job.id} | {self.job.filename}\n",
                encoding="utf-8")
        with self.align_log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def check(self) -> None:
        self.job.check_cancel()

    # ------------------------------------------------------------------ stages
    def run(self) -> Dict:
        try:
            info = self._prepare()
            segments = self._vad()
            source_items = self._recognize(segments)
            target_items = self._translate(source_items)
            clips = self._dubbing(target_items)
            audio_path, target_items = self._align(clips, target_items, info)
            result = self._assemble(audio_path, target_items, info)
            self._save_edit_project(clips, target_items, info)
            return result
        finally:
            # Job hỏng hoặc bị huỷ giữa chừng thì token đã tiêu vẫn phải được báo:
            # tiền đã mất rồi, giấu đi chỉ khiến người dùng tưởng là không tốn.
            self._report_tokens()

    # 1. Chuẩn bị: tách audio 16k + video không tiếng
    def _prepare(self) -> Dict:
        self.stage("Chuẩn bị dữ liệu", 3)
        source = Path(self.job.source_path)
        info = ffmpeg.probe(source)
        if not info["has_audio"]:
            raise PipelineError("File không có luồng âm thanh hợp lệ, không thể xử lý.")
        self.total_ms = info["duration_ms"]
        self.log(f"Đầu vào: {source.name} | {info['duration_ms'] / 1000:.1f}s | "
                 f"{'video ' + str(info['width']) + 'x' + str(info['height']) if info['has_video'] else 'chỉ audio'}")

        self.audio16k = self.cache / "source_16k.wav"
        asr_source = self._separate_vocals(source)
        ffmpeg.extract_audio_16k(asr_source, self.audio16k)
        self.log("Đã tách audio 16kHz mono để nhận dạng")
        self.check()

        self.novoice = None
        if info["has_video"]:
            self.stage("Tách video không tiếng", 6)
            self.novoice = self.cache / "novoice.mp4"
            ffmpeg.extract_video_only(source, self.novoice, info)
            self.log("Đã tách phần hình ảnh (novoice.mp4)")
        return info

    def _separate_vocals(self, source: Path) -> Path:
        """Tách giọng khỏi nhạc nền trước khi VAD/ASR.

        Trả về file dùng làm nguồn cho ASR: stem giọng nếu tách được, không thì
        chính file gốc. Stem nhạc nền được giữ ở self.accompaniment để bước ghép
        cuối trộn lại - nhờ vậy nhạc nền không mất mà cũng không kéo theo giọng
        gốc như khi trộn nguyên audio nguồn.
        """
        if not self.separate_enabled:
            return source
        client = separate.SeparatorClient(self.separate_url, timeout=self.separate_timeout)
        if not client.available():
            self.log("Service tách nhạc không chạy, dùng audio gốc (nhạc nền có thể "
                     "bị nhận nhầm là thoại). Chạy ./install_aligner.sh để bật.", "warn")
            return source
        self.stage("Tách giọng khỏi nhạc nền", 4)
        vocals = self.cache / "vocals.wav"
        accompaniment = self.cache / "accompaniment.wav"
        started = time.time()
        try:
            info = client.separate(source, vocals, accompaniment)
        except separate.SeparateError as exc:
            self.log(f"Tách nhạc thất bại, dùng audio gốc: {exc}", "warn")
            return source
        self.accompaniment = accompaniment if accompaniment.is_file() else None
        # Giữ luôn stem giọng gốc: cho phép phát giọng gốc cùng lúc với giọng lồng
        # tiếng, với âm lượng riêng (kiểu voice-over/thuyết minh chồng tiếng)
        self.vocals = vocals if vocals.is_file() else None
        self.log(f"Đã tách giọng khỏi nhạc nền trong {time.time() - started:.1f}s "
                 f"({info.get('duration', 0):.0f}s audio) - ASR sẽ chạy trên giọng đã sạch nhạc")
        self.check()
        return vocals

    # 2. VAD: cắt thành các đoạn có tiếng nói
    def _vad(self) -> List:
        self.stage("Cắt câu bằng VAD", 9)
        result = vad.analyze(
            str(self.audio16k),
            min_speech_ms=self.min_speech_ms,
            max_speech_ms=self.max_speech_ms,
            min_silence_ms=self.min_silence_ms,
            music_filter=self.music_filter,
            speech_score_min=self.speech_score_min,
            music_gap_ms=self.music_gap_ms,
            subtract_floor=self.subtract_music_floor,
        )
        segments = result["segments"]
        self._log_vad_music(result)
        if not segments:
            if result["dropped"]:
                raise PipelineError(
                    "Không tìm thấy giọng nói: toàn bộ file bị bộ lọc chấm là nhạc/tiếng động. "
                    "Nếu chắc chắn có thoại, hạ pipeline.speech_score_min (mặc định 0.45) "
                    "hoặc tắt pipeline.music_filter trong cài đặt.")
            raise PipelineError("Không phát hiện được giọng nói nào trong file.")
        total = sum(e - s for s, e in segments) / 1000.0
        self.log(f"VAD tìm thấy {len(segments)} đoạn thoại, tổng {total:.1f}s")
        return segments

    def _log_vad_music(self, result: Dict) -> None:
        """Báo lên UI phần bị bộ lọc nhạc gạt đi, kèm chi tiết vào align-debug.log."""
        if not self.music_filter:
            return
        if result.get("fallback"):
            self.log("Bộ lọc nhạc bị vô hiệu (nó định vứt gần hết file) - giữ nguyên kết quả VAD.",
                     "warn")
            return
        dropped, trimmed, split = result["dropped"], result["trimmed"], result["split"]
        if not (dropped or trimmed or split):
            return
        music_ms = sum(
            (info["end"] - info["start"]) - sum(b - a for a, b in info.get("pieces", []))
            for info in result["report"])
        self.log(f"Bộ lọc nhạc nền: bỏ {dropped} đoạn, gọt {trimmed}, cắt đôi {split} "
                 f"- loại tổng cộng {music_ms / 1000:.1f}s không phải giọng nói")
        lines = ["", "# Bộ lọc nhạc nền (điểm càng cao càng giống giọng nói)"]
        for info in result["report"]:
            if info["action"] == "keep":
                continue
            pieces = ", ".join(f"{_ts(a)}->{_ts(b)}" for a, b in info.get("pieces", [])) or "(bỏ hết)"
            lines.append(f"  {_ts(info['start'])}->{_ts(info['end'])} [{info['action']}] "
                         f"score={info['score']:.2f} "
                         f"win={info['speech_windows']}/{info['windows']} | {pieces}")
        self._debug(lines)

    # 3. Nhận dạng bằng Loli 2.0
    def _recognize(self, segments: List) -> List[Dict]:
        self.stage("Nhận dạng giọng nói (Loli 2.0)", 12)
        stt_cfg = self.config.get("stt", {})
        client = LoliSTT(stt_cfg.get("base_url", ""), stt_cfg.get("api_key", ""))
        language = self.params.get("source_lang") or "auto"
        clip_dir = self.cache / "asr"
        clip_dir.mkdir(exist_ok=True)

        texts: List[str] = [""] * len(segments)
        detected_languages: List[str] = [""] * len(segments)
        done = {"n": 0}
        step = 1 if len(segments) <= 25 else 5
        fatal: List[Exception] = []

        def work(index: int) -> None:
            if fatal or self.job.cancelled:
                return
            start, end = segments[index]
            clip = clip_dir / f"seg_{index:04d}.wav"
            try:
                ffmpeg.cut_audio(self.audio16k, start, end, clip)
                result = client.transcribe(clip, language=language)
                texts[index] = result.get("text", "")
                detected_languages[index] = result.get("language") or language
            except SttError as exc:
                if exc.fatal:
                    fatal.append(exc)
                    return
                self.log(f"Đoạn {index + 1} nhận dạng lỗi, bỏ qua: {exc}", "warn")
            except ffmpeg.FFmpegError as exc:
                self.log(f"Đoạn {index + 1} cắt audio lỗi, bỏ qua: {exc}", "warn")
            finally:
                done["n"] += 1
                count = done["n"]
                self.stage("Nhận dạng giọng nói (Loli 2.0)",
                           12 + int(33 * count / max(1, len(segments))))
                if count % step == 0 or count == len(segments):
                    self.log(f"Đã nhận dạng {count}/{len(segments)} đoạn")

        with ThreadPoolExecutor(max_workers=self.stt_workers) as pool:
            futures = [pool.submit(work, i) for i in range(len(segments))]
            for future in futures:
                future.result()
                self.check()
        if fatal:
            raise PipelineError(f"Lỗi STT: {fatal[0]}")

        items = []
        for i, (start, end) in enumerate(segments):
            item = srt.make_item(i + 1, start, end, texts[i])
            item["_language"] = detected_languages[i] or language
            items.append(item)
        items = srt.clean_and_fix(items)
        if not items:
            raise PipelineError("Nhận dạng không ra nội dung nào. Kiểm tra lại ngôn ngữ nguồn hoặc file audio.")
        items, hallucinated = srt.drop_hallucinations(items)
        if hallucinated:
            self.log(f"Bỏ {len(hallucinated)} dòng ASR bịa ra từ nhạc/tiếng động")
            self._debug(["", "# Dòng ASR bịa ra (nhận vào nhạc nền, không có tiếng người)"] +
                        [f"  [{h['line']:>4}] {_ts(h['start_ms'])} -> {_ts(h['end_ms'])} "
                         f"{h['text']!r}  <- {h['_drop_reason']}" for h in hallucinated])
        join_flag = "" if (self.params.get("source_lang") or "").split("-")[0] in CJK_LANGS else " "
        items = srt.merge_short(items, min_ms=self.min_speech_ms, max_ms=self.max_speech_ms,
                                join_flag=join_flag)
        items = self._forced_align(items)
        items = self._resegment(items, join_flag)

        self.source_srt = self.output / "source.srt"
        self.source_srt.write_text(srt.to_srt(items), encoding="utf-8")
        self.log(f"Phụ đề gốc: {len(items)} dòng -> source.srt")
        self.job.set_preview("source_srt", srt.to_srt(items))
        return items

    def _forced_align(self, items: List[Dict]) -> List[Dict]:
        """Dùng transcript Loli + Qwen để tinh chỉnh timestamp và xuất words.json."""
        cfg = self.config.get("aligner", {})
        self.word_timestamps = None
        if not bool(cfg.get("enabled", True)):
            self.log("Qwen Forced Aligner đang tắt; giữ timestamp VAD")
            return items

        source_code = self.params.get("source_lang") or "auto"
        fixed_language = supported_language(source_code)
        item_languages = [fixed_language or supported_language(it.get("_language", "")) for it in items]
        if not any(item_languages):
            self.log(f"Qwen Forced Aligner chưa hỗ trợ ngôn ngữ nguồn '{source_code}'; giữ timestamp VAD", "warn")
            return items

        client = QwenForcedAlignerClient(
            cfg.get("base_url", "http://127.0.0.1:8200"),
            timeout=float(cfg.get("timeout", 300)),
        )
        try:
            health = client.health()
            self.log(f"Qwen Forced Aligner: {health.get('model')} trên {health.get('device')}"
                     + (" (đã load)" if health.get("loaded") else " (đang load model lần đầu)"))
        except ForcedAlignerError as exc:
            self.log(f"Không kết nối được Qwen Forced Aligner; giữ timestamp VAD: {exc}", "warn")
            return items

        self.stage("Căn timestamp từng từ (Qwen3 Forced Aligner)", 43)
        aligned_items: List[Dict] = []
        all_words: List[Dict] = []
        segments_report: List[Dict] = []
        used_languages = set()
        suspicious = 0
        align_dir = self.cache / "aligner"
        align_dir.mkdir(exist_ok=True)

        # UI đọc trực tiếp list này nên bảng word-level hiện dần ngay trong lúc chạy
        self.word_report = {
            "model": health.get("model", ""),
            "language": [],
            "segments": segments_report,
            "done": False,
        }
        self.job.set_words(self.word_report)

        self._debug([
            "",
            "=" * 92,
            f"WORD-LEVEL TIMESTAMPS | model={health.get('model')} | device={health.get('device')}",
            "rel = mốc trong clip aligner nhận được, abs = mốc trên timeline video gốc",
            "=" * 92,
        ])

        for index, original in enumerate(items):
            self.check()
            item = dict(original)
            language = item_languages[index]
            vad_start, vad_end = item["start_ms"], item["end_ms"]
            clip_ms = max(0, vad_end - vad_start)
            report = {
                "line": item["line"],
                "text": item["text"],
                "language": language or "",
                "vad": {"start": round(vad_start / 1000.0, 3), "end": round(vad_end / 1000.0, 3)},
                "clip_duration": round(clip_ms / 1000.0, 3),
                "source": "vad",
                "words": [],
                "warnings": [],
            }
            if not language:
                report["warnings"].append("ngôn ngữ không được Qwen hỗ trợ")
                self.log(f"Dòng {item['line']}: ngôn ngữ không được Qwen hỗ trợ; giữ VAD", "warn")
                item["_report"] = report
                aligned_items.append(item)
                segments_report.append(report)
                self.word_segments[item["line"]] = report
                self._debug_segment(report)
                continue
            used_languages.add(language)
            clip = align_dir / f"align_{index:04d}.wav"
            try:
                ffmpeg.cut_audio(self.audio16k, vad_start, vad_end, clip)
                words = client.align(clip, item["text"], language)
                # Aligner phải phủ kín clip nên hay dán một từ lên cả đoạn nhạc nền
                # ở đầu/cuối. So với trung vị giây/ký tự để phát hiện và co lại.
                stretch_notes: List[Dict] = []
                if self.fix_stretched:
                    words, stretch_notes = wordfix.fix_stretched(
                        words, self._rates.rate(words),
                        factor=self.stretch_factor,
                        min_stretch_s=self.stretch_min_ms / 1000.0,
                        clip_s=clip_ms / 1000.0,
                    )
                    self._rates.observe(words)
                    for note in stretch_notes:
                        self.log(f"Dòng {item['line']}: nắn từ bị kéo dãn "
                                 f"{wordfix.describe(note)}", "warn")
                fixes = {note["index"]: note for note in stretch_notes}
                lone = set(wordfix.lone_edges(words)) - set(fixes)
                absolute = []
                detail = []
                prev_end_ms = -1
                for order, word in enumerate(words):
                    rel_start_ms = int(float(word.get("start", 0)) * 1000)
                    rel_end_ms = int(float(word.get("end", 0)) * 1000)
                    start_ms = vad_start + rel_start_ms
                    end_ms = vad_start + rel_end_ms
                    text = str(word.get("word", ""))
                    flags = []
                    if rel_end_ms > clip_ms + self.overshoot_warn_ms:
                        flags.append("VƯỢT-CLIP")
                    if rel_start_ms < prev_end_ms:
                        flags.append("CHỒNG-TỪ-TRƯỚC")
                    if rel_end_ms <= rel_start_ms:
                        flags.append("ĐỘ-DÀI<=0")
                    note = fixes.get(order)
                    if note:
                        flags.append("KÉO-DÃN-ĐÃ-NẮN")
                    if order in lone:
                        flags.append("ĐỨNG-LẺ-XA-CỤM")
                    prev_end_ms = max(prev_end_ms, rel_end_ms)
                    entry = {
                        "word": text,
                        "rel_start": round(rel_start_ms / 1000.0, 3),
                        "rel_end": round(rel_end_ms / 1000.0, 3),
                        "start": round(start_ms / 1000.0, 3),
                        "end": round(end_ms / 1000.0, 3),
                        "flags": flags,
                    }
                    if note:
                        entry["fixed_from"] = {"rel_start": note["from"][0],
                                               "rel_end": note["from"][1],
                                               "anchor": note["anchor"]}
                    detail.append(entry)
                    if end_ms <= start_ms:
                        continue
                    absolute.append({
                        "word": text,
                        "start": round(start_ms / 1000.0, 3),
                        "end": round(end_ms / 1000.0, 3),
                        "line": item["line"],
                    })
                report["words"] = detail
                # KÉO-DÃN-ĐÃ-NẮN là thông tin, không phải lỗi còn tồn: đã log riêng ở trên
                bad = sorted({flag for word in detail for flag in word["flags"]}
                             - {"KÉO-DÃN-ĐÃ-NẮN", "ĐỨNG-LẺ-XA-CỤM"})
                if bad:
                    suspicious += 1
                    report["warnings"].extend(bad)
                    self.log(f"Dòng {item['line']}: timestamp aligner đáng ngờ ({', '.join(bad)}), "
                             f"xem align-debug.log", "warn")
                if absolute:
                    # Chừa biên nhỏ để không cắt phụ âm đầu/cuối.
                    new_start = max(0, int(absolute[0]["start"] * 1000) - 40)
                    new_end = min(self.total_ms, int(absolute[-1]["end"] * 1000) + 40)
                    item["start_ms"] = new_start
                    item["end_ms"] = new_end
                    item["source_words"] = absolute
                    all_words.extend(absolute)
                    report["source"] = "aligner"
                    report["aligned"] = {"start": round(new_start / 1000.0, 3),
                                         "end": round(new_end / 1000.0, 3)}
                    report["shift"] = {"start": round((new_start - vad_start) / 1000.0, 3),
                                       "end": round((new_end - vad_end) / 1000.0, 3)}
                else:
                    report["warnings"].append("aligner không trả timestamp")
                    self.log(f"Dòng {item['line']}: aligner không trả timestamp; giữ VAD", "warn")
            except (ForcedAlignerError, ffmpeg.FFmpegError, ValueError) as exc:
                report["warnings"].append(f"lỗi: {exc}")
                self.log(f"Dòng {item['line']}: forced alignment lỗi, giữ VAD: {exc}", "warn")
            item["_report"] = report
            aligned_items.append(item)
            segments_report.append(report)
            self.word_segments[item["line"]] = report
            self._debug_segment(report)
            if self.align_debug_ui and report["words"]:
                for word in report["words"]:
                    self.log(f"  [dòng {report['line']}] {_ts(word['start'] * 1000)} -> "
                             f"{_ts(word['end'] * 1000)}  {word['word']}")
            self.stage("Căn timestamp từng từ (Qwen3 Forced Aligner)",
                       43 + int(2 * (index + 1) / max(1, len(items))))

        self.word_report["language"] = sorted(used_languages)
        self.word_report["done"] = True
        if all_words:
            self.word_timestamps = self.output / "word-timestamps.json"
            payload = {
                "model": health.get("model", "Qwen/Qwen3-ForcedAligner-0.6B"),
                "language": sorted(used_languages),
                "segments": segments_report,
                "words": all_words,
            }
            self.word_timestamps.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            shifted = [seg for seg in segments_report if seg.get("shift")]
            avg_shift = (sum(abs(seg["shift"]["start"]) for seg in shifted) / len(shifted)) if shifted else 0.0
            worst = max(shifted, key=lambda seg: abs(seg["shift"]["start"]), default=None)
            self.log(f"Đã căn {len(all_words)} từ/ký tự bằng Qwen3 Forced Aligner "
                     f"({len(shifted)}/{len(items)} dòng dùng mốc aligner)")
            self.log(f"Dịch mốc bắt đầu so với VAD: trung bình {avg_shift:.2f}s"
                     + (f", lớn nhất {abs(worst['shift']['start']):.2f}s ở dòng {worst['line']}"
                        if worst else ""))
            if suspicious:
                self.log(f"{suspicious} dòng có timestamp từ đáng ngờ - đây thường là nguyên nhân "
                         f"chèn lệch, xem align-debug.log", "warn")
        return aligned_items

    def _translator(self) -> OpenAITranslator:
        """Một chỗ dựng translator cho cả chấm câu lẫn dịch."""
        if getattr(self, "_translator_cache", None) is None:
            cfg = self.config.get("openai", {})
            self._translator_cache = OpenAITranslator(
                api_key=cfg.get("api_key", ""),
                base_url=cfg.get("base_url", ""),
                model=cfg.get("model", ""),
                temperature=cfg.get("temperature", 0.3),
                batch_size=cfg.get("batch_size", 20),
                context_chars=int(cfg.get("context_chars", 12000)),
                keep_terms=cfg.get("keep_terms") or [],
                # Chỉ thị riêng cho job đè lên chỉ thị chung trong cấu hình
                instruction=(self.params.get("instruction")
                             or cfg.get("instruction") or ""),
            )
        return self._translator_cache

    def _source_language_name(self, items: List[Dict]) -> str:
        """Tên tiếng Anh của ngôn ngữ nguồn để nói thẳng trong prompt.

        Ngôn ngữ đặt 'auto' thì lấy ngôn ngữ mà ASR nhận ra ở nhiều dòng nhất,
        có vậy prompt mới nói được 'from English into Vietnamese'.
        """
        code = (self.params.get("source_lang") or "auto").split("-")[0]
        if code and code != "auto":
            return english_name(code)
        counts: Dict[str, int] = {}
        for item in items:
            detected = (item.get("_language") or "").split("-")[0]
            if detected and detected != "auto":
                counts[detected] = counts.get(detected, 0) + 1
        if not counts:
            return ""
        return english_name(max(counts, key=counts.get))

    # 3b. Tách khối ASR thành từng câu, lấy mốc thật từ word-level timestamps
    def _resegment(self, items: List[Dict], join_flag: str) -> List[Dict]:
        cfg = self.config.get("resegment", {})
        if not bool(cfg.get("enabled", True)):
            for item in items:
                item.pop("_report", None)
            return items
        if not any(item.get("source_words") for item in items):
            self.log("Chưa có word-level timestamps nên bỏ qua bước tách câu", "warn")
            for item in items:
                item.pop("_report", None)
            return items

        llm = None
        if bool(cfg.get("llm_assist", True)) and self.config.get("openai", {}).get("api_key"):
            translator = self._translator()
            source_name = self._source_language_name(items)

            def llm(item: Dict) -> Optional[List[str]]:
                # Chỉ gọi khi trong câu không có dấu kết nào để mà cắt
                if len(resegment.sentences_of(item["text"])) > 1:
                    return None
                return translator.split_sentences(item["text"], source_name)

        before = len(items)
        self.stage("Tách câu theo mốc từng từ", 44)
        split = resegment.split_items(
            items,
            min_piece_ms=int(cfg.get("min_piece_ms", 400)),
            margin_ms=int(cfg.get("margin_ms", 40)),
            join_flag=join_flag,
            llm=llm,
            llm_min_ms=int(cfg.get("llm_min_ms", 6000)),
            log=self.log,
        )
        split = srt.clean_and_fix(split)
        if len(split) != before:
            self._debug_split(items, split)
        self._rebuild_word_report(split)
        for item in split:
            item.pop("_report", None)
        return split

    def _debug_split(self, before: List[Dict], after: List[Dict]) -> None:
        lines = ["", "=" * 92,
                 f"TÁCH CÂU THEO MỐC TỪ: {len(before)} dòng -> {len(after)} dòng",
                 "=" * 92]
        for item in after:
            parent = (item.get("_report") or {}).get("line")
            origin = f" (từ dòng gốc {parent})" if parent else ""
            lines.append(f"[dòng {item['line']:>4}] {_ts(item['start_ms'])} -> {_ts(item['end_ms'])}"
                         f"{origin}  {item['text']}")
        self._debug(lines)

    def _rebuild_word_report(self, items: List[Dict]) -> None:
        """Dựng lại báo cáo word-level theo số dòng sau khi tách, để UI khớp source.srt."""
        if not self.word_report:
            return
        segments: List[Dict] = []
        self.word_segments = {}
        seen_parents = set()
        # Chỉ dòng nào thật sự sinh ra từ một khối bị cắt mới được gắn nhãn "tách từ khối N"
        piece_count: Dict[int, int] = {}
        for item in items:
            parent = item.get("_report")
            if parent:
                piece_count[parent["line"]] = piece_count.get(parent["line"], 0) + 1
        for item in items:
            parent = item.get("_report")
            if not parent:
                continue
            first = parent["line"] not in seen_parents
            seen_parents.add(parent["line"])
            low, high = item["start_ms"] / 1000.0, item["end_ms"] / 1000.0
            words = [w for w in parent.get("words", []) if low <= w["start"] <= high]
            flags = sorted({flag for word in words for flag in word.get("flags", [])})
            # Cảnh báo cấp dòng của khối gốc chỉ gắn vào mảnh đầu tiên
            inherited = [w for w in parent.get("warnings", []) if w not in flags] if first else []
            report = {
                "line": item["line"],
                "text": item["text"],
                "language": parent.get("language", ""),
                "vad": parent.get("vad", {}),
                "clip_duration": parent.get("clip_duration", 0),
                "source": parent.get("source", "vad"),
                "words": words,
                "warnings": flags + inherited,
            }
            if parent.get("aligned"):
                report["aligned"] = {"start": round(low, 3), "end": round(high, 3)}
                report["shift"] = parent["shift"] if first else {"start": 0.0, "end": 0.0}
            if piece_count.get(parent["line"], 1) > 1:
                report["split_of"] = parent["line"]
            segments.append(report)
            self.word_segments[item["line"]] = report
        if segments:
            self.word_report["segments"] = segments
            self.job.set_words(self.word_report)

    def _debug_segment(self, report: Dict) -> None:
        """Đổ chi tiết một câu ra align-debug.log."""
        if not self.align_debug:
            return
        vad = report["vad"]
        head = (f"\n[dòng {report['line']:>4}] VAD {_ts(vad['start'] * 1000)} -> {_ts(vad['end'] * 1000)}"
                f" ({report['clip_duration']:.3f}s) | nguồn mốc: {report['source']}"
                f" | ngôn ngữ: {report['language'] or '-'}")
        lines = [head]
        if report.get("aligned"):
            aligned, shift = report["aligned"], report["shift"]
            lines.append(f"            ALIGN {_ts(aligned['start'] * 1000)} -> {_ts(aligned['end'] * 1000)}"
                         f" ({aligned['end'] - aligned['start']:.3f}s)"
                         f" | Δbắt đầu {shift['start']:+.3f}s | Δkết thúc {shift['end']:+.3f}s")
        lines.append(f"            text: {report['text']}")
        if report["warnings"]:
            lines.append(f"            !! {'; '.join(str(w) for w in report['warnings'])}")
        for order, word in enumerate(report["words"], 1):
            flag = ("  << " + ", ".join(word["flags"])) if word["flags"] else ""
            lines.append(f"      {order:>3}. rel {word['rel_start']:8.3f} -> {word['rel_end']:8.3f}"
                         f" | abs {_ts(word['start'] * 1000)} -> {_ts(word['end'] * 1000)}"
                         f" | {word['word']}{flag}")
            was = word.get("fixed_from")
            if was:
                lines.append(f"           ^ aligner trả {was['rel_start']:.3f} -> {was['rel_end']:.3f},"
                             f" đã nắn về sát cụm từ phía {was['anchor']}")
        self._debug(lines)

    # 4. Dịch bằng OpenAI
    def _translate(self, items: List[Dict]) -> List[Dict]:
        target_lang = self.params.get("target_lang") or "vi"
        source_lang = (self.params.get("source_lang") or "auto").split("-")[0]
        self.target_srt = self.output / "target.srt"

        if target_lang == source_lang:
            self.log("Ngôn ngữ đích trùng ngôn ngữ nguồn, bỏ qua bước dịch")
            for item in items:
                item["source_text"] = item["text"]
            self.target_srt.write_text(srt.to_srt(items), encoding="utf-8")
            self.job.set_preview("target_srt", srt.to_srt(items))
            return items

        self.stage("Dịch phụ đề (OpenAI)", 45)
        translator = self._translator()
        source_name = self._source_language_name(items)
        self.log(f"Dịch {english_name(source_lang) if source_lang != 'auto' else source_name or 'ngôn ngữ nguồn'}"
                 f" -> {english_name(target_lang)} | model {translator.model}"
                 f" | ngữ cảnh: cả transcript {len(items)} dòng"
                 + (f" | giữ nguyên {len(translator.keep_terms)} thuật ngữ chỉ định"
                    if translator.keep_terms else ""))
        if translator.instruction:
            source = "riêng cho video này" if self.params.get("instruction") else "chung trong cấu hình"
            preview = " ".join(translator.instruction.split())
            self.log(f"Áp dụng chỉ thị dịch {source} ({len(translator.instruction)} ký tự): "
                     + (preview[:160] + "…" if len(preview) > 160 else preview))
            self._debug(["", f"# Chỉ thị dịch ({source})", translator.instruction])

        def progress(index: int, total: int, message: str) -> None:
            self.stage("Dịch phụ đề (OpenAI)", 45 + int(15 * index / max(1, total)))
            self.log(message)
            self.job.check_cancel()

        # Cho LLM biết mỗi câu có bao nhiêu giây để nói: câu dịch quá dài là nguyên nhân
        # lệch tiếng mà không tốc độ nào cứu được
        budgets = {it["line"]: b for it, b in zip(items, self._budgets(items, self.total_ms))}
        try:
            translated = translator.translate(
                items,
                target_name=english_name(target_lang),
                source_name=source_name,
                progress=progress,
                budgets=budgets,
            )
        except TranslateError as exc:
            raise PipelineError(f"Lỗi dịch: {exc}") from exc

        translated = srt.align_by_time(items, translated)
        # align_by_time tạo dict mới; mang theo mốc từ của giọng gốc để log căn chỉnh đối chiếu được
        source_words = {it["line"]: it.get("source_words") for it in items}
        source_text = {it["line"]: it["text"] for it in items}
        for item in translated:
            words = source_words.get(item["line"])
            if words:
                item["source_words"] = words
            item["source_text"] = source_text.get(item["line"], "")
        self.target_srt.write_text(srt.to_srt(translated), encoding="utf-8")
        self._report_tokens()
        self.log(f"Đã dịch sang {english_name(target_lang)} -> target.srt")
        self.job.set_preview("target_srt", srt.to_srt(translated))
        return translated

    # ----------------------------------------------------------- khung thời gian
    def _report_tokens(self) -> None:
        """Báo số token OpenAI đã dùng - lấy thẳng từ trường usage của response.

        Số token là con số OpenAI dùng để tính tiền nên luôn chính xác. Tiền chỉ
        hiện khi model có trong bảng giá openai.pricing; không có thì im lặng bỏ
        qua, thà thiếu còn hơn hiện số bịa.
        """
        translator = getattr(self, "_translator_cache", None)
        if translator is None:
            return
        usage = translator.usage.snapshot()
        if not usage["calls"] and not usage.get("missing_usage"):
            return
        if self._tokens_reported == (usage["calls"], usage.get("missing_usage", 0)):
            return      # không có request mới kể từ lần báo trước
        self._tokens_reported = (usage["calls"], usage.get("missing_usage", 0))
        self.token_usage = usage
        parts = [f"{usage['total_tokens']:,} token "
                 f"(vào {usage['prompt_tokens']:,}, ra {usage['completion_tokens']:,}"]
        if usage["cached_tokens"]:
            share = usage["cached_tokens"] / max(1, usage["prompt_tokens"]) * 100
            parts.append(f", trong đó {usage['cached_tokens']:,} token vào được cache "
                         f"= {share:.0f}% giá rẻ")
        parts.append(f") qua {usage['calls']} request")
        line = f"OpenAI đã dùng {''.join(parts)}"

        cost = estimate_cost(usage, translator.model,
                             self.config.get("openai", {}).get("pricing", {}))
        if cost:
            self.token_cost = cost
            line += f" | ước tính {cost['total_usd']:.4f} USD theo bảng giá cho {cost['model']}"
        else:
            line += (f" | chưa có giá cho model {translator.model} trong openai.pricing "
                     f"nên không quy ra tiền")
        self.log(line)
        missing = usage.get("missing_usage") or 0
        if missing:
            total_calls = usage["calls"] + missing
            self.log(f"CẢNH BÁO: {missing}/{total_calls} request trả về không kèm trường "
                     f"`usage`, phần token của chúng KHÔNG nằm trong con số trên. "
                     f"Số thực tế cao hơn. Thường gặp khi openai.base_url trỏ vào proxy "
                     f"tương thích thay vì api.openai.com.", "warn")

        detail = ["", "# Token OpenAI (số thật từ trường usage của API)"]
        for purpose, bucket in usage["by_purpose"].items():
            detail.append(f"  {purpose:<16} {bucket['calls']:>3} request | "
                          f"vào {bucket['prompt']:>8,} (cache {bucket['cached']:>8,}) | "
                          f"ra {bucket['completion']:>7,}")
        if missing:
            detail.append(f"  !! {missing} request không trả usage -> token của chúng bị thiếu khỏi tổng")
        if cost:
            detail.append(f"  giá áp dụng (USD/1M token): vào {cost['rates']['input']}, "
                          f"cache {cost['rates']['cached_input']}, ra {cost['rates']['output']}")
            detail.append(f"  thành tiền: vào {cost['input_usd']:.6f} + cache {cost['cached_usd']:.6f} "
                          f"+ ra {cost['output_usd']:.6f} = {cost['total_usd']:.6f} USD")
        self._debug(detail)

    def _budgets(self, items: List[Dict], total_ms: int) -> List[int]:
        """Thời lượng tối đa mỗi câu được phép chiếm.

        Hai ràng buộc, lấy cái chặt hơn:
        - tới lúc câu sau bắt đầu, chừa một nhịp thở;
        - **không đọc quá lúc giọng gốc đã dứt**, cộng dung sai `max_overrun_ms`.

        Ràng buộc thứ hai mới là cái quyết định. Khoảng lặng sau một câu là lúc
        nhân vật đã ngậm miệng; mượn chỗ đó để đọc nốt thì câu vẫn "vừa khung"
        nhưng tiếng chạy dài quá hình, không thuật toán căn chỉnh nào cứu được.
        """
        budgets = []
        for i, item in enumerate(items):
            next_start = items[i + 1]["start_ms"] if i + 1 < len(items) else max(item["end_ms"], total_ms)
            room = next_start - item["start_ms"] - self.gap_reserve_ms
            spoken = item["end_ms"] - item["start_ms"] + self.max_overrun_ms
            budgets.append(max(400, min(room, spoken)))
        return budgets

    # 5. Lồng tiếng bằng Loly 3.5
    def _dubbing(self, items: List[Dict]) -> List[Dict]:
        self.stage("Tổng hợp giọng nói (Loly 3.5)", 60)
        tts_cfg = self.config.get("tts", {})
        voice_id = (self.params.get("voice_id") or tts_cfg.get("voice_id") or "").strip()
        client = LolyTTS(tts_cfg.get("base_url", ""), tts_cfg.get("api_key", ""), voice_id)

        if self.params.get("clone_voice"):
            cloned = self._clone_source_voice(client, items)
            if cloned:
                client.voice_id = cloned

        language = self.params.get("target_lang") or "auto"
        base_speed = _clamp(float(self.params.get("speed") or tts_cfg.get("speed", 1.0)),
                            TTS_SPEED_MIN, TTS_SPEED_MAX)
        dit_steps = int(self.params.get("dit_steps") or tts_cfg.get("dit_steps", 16))
        autorate = bool(self.params.get("voice_autorate", True))
        resynth_enabled = bool(self.params.get("resynth", True)) and autorate
        budgets = self._budgets(items, self.total_ms)

        clip_dir = self.cache / "tts"
        clip_dir.mkdir(exist_ok=True)
        self.log(f"Lồng tiếng {len(items)} câu | dit_steps={dit_steps} "
                 f"(chi phí ×{dit_steps / 8:.2f}) | tốc độ nền {base_speed:.2f}"
                 + (" | tự khớp tốc độ theo khung thời gian" if autorate else " | không đổi tốc độ"))

        clips: List[Dict] = [dict(it, audio=None, dub_ms=0, speed=base_speed, budget=budgets[i])
                             for i, it in enumerate(items)]
        done = {"n": 0}
        stats = {"resynth": 0, "predicted": 0, "binary": 0}
        step = 1 if len(items) <= 25 else 5
        fatal: List[Exception] = []

        def synth(index: int, speed: float, tag: str = "", update_rate: bool = True,
                  target_duration: Optional[float] = None):
            """Sinh audio, cắt lặng hai đầu, ghi đè lại file và trả về thời lượng thật."""
            item = items[index]
            out_file = clip_dir / f"dub_{index:04d}{tag}.wav"
            client.synthesize(item["text"], out_file, language=language,
                              speed=round(speed, 3), dit_steps=dit_steps, fmt="wav",
                              duration=target_duration)
            samples = _trim_silence(ffmpeg.decode_pcm(out_file))
            ffmpeg.write_wav(samples, out_file)
            duration = _duration_ms(samples)
            if update_rate:
                self.rate.update(len(item["text"]), duration, speed)
            clips[index]["audio"] = str(out_file)
            clips[index]["dub_ms"] = duration
            clips[index]["speed"] = speed
            return duration, str(out_file)

        def work(index: int) -> None:
            if fatal or self.job.cancelled:
                return
            item = items[index]
            budget = budgets[index]
            speed = base_speed
            duration_mode = 0.5 <= budget / 1000.0 <= 30.0
            try:
                # Đoán trước độ dài từ tốc độ đọc thực tế của các câu đã sinh
                if autorate:
                    predicted = self.rate.predict_ms(len(item["text"]), base_speed)
                    if predicted and predicted > budget + self.fit_tolerance_ms:
                        speed = _clamp(base_speed * predicted / budget, base_speed, TTS_SPEED_MAX)
                        if speed > base_speed + 0.02:
                            stats["predicted"] += 1
                        else:
                            speed = base_speed

                duration, selected_file = synth(
                    index, speed, "_initial", target_duration=budget / 1000.0
                    if duration_mode else None)

                # Duration là cơ chế chính xác của Dubbing Studio. Nếu sai số vẫn
                # vượt ngưỡng, binary search trên duration; chỉ fallback sang speed
                # cho đoạn không thể dùng duration (khung > 30 giây).
                if (resynth_enabled and duration > budget + self.fit_tolerance_ms
                        and (duration_mode or speed < TTS_SPEED_MAX - 0.02)):
                    best = (abs(duration - budget), duration, selected_file, speed)
                    low, high = (0.5, budget / 1000.0) if duration_mode else (speed, TTS_SPEED_MAX)
                    for trial in range(self.binary_iterations):
                        trial_value = (low + high) / 2.0
                        trial_speed = trial_value if not duration_mode else speed
                        trial_duration, trial_file = synth(
                            index, trial_speed, f"_bs{trial}", update_rate=False,
                            target_duration=trial_value if duration_mode else None)
                        candidate = (abs(trial_duration - budget), trial_duration,
                                     trial_file, trial_speed)
                        if candidate[0] < best[0]:
                            best = candidate
                        if trial_duration > budget:
                            low = trial_speed
                        else:
                            high = trial_speed
                        if trial_duration <= budget + self.fit_tolerance_ms:
                            break
                    _, duration, selected_file, speed = best
                    final_file = clip_dir / f"dub_{index:04d}.wav"
                    shutil.copy2(selected_file, final_file)
                    clips[index]["audio"] = str(final_file)
                    clips[index]["dub_ms"] = duration
                    clips[index]["speed"] = speed
                    stats["resynth"] += 1
                    stats["binary"] += 1
            except TtsError as exc:
                if exc.fatal:
                    fatal.append(exc)
                    return
                self.log(f"Dòng {item['line']} lồng tiếng lỗi, để im lặng: {exc}", "warn")
            except ffmpeg.FFmpegError as exc:
                self.log(f"Dòng {item['line']} xử lý audio lỗi, để im lặng: {exc}", "warn")
            finally:
                done["n"] += 1
                count = done["n"]
                self.stage("Tổng hợp giọng nói (Loly 3.5)",
                           60 + int(25 * count / max(1, len(items))))
                if count % step == 0 or count == len(items):
                    self.log(f"Đã tổng hợp {count}/{len(items)} câu")

        # Sinh vài câu đầu tuần tự để có số liệu tốc độ đọc trước khi chạy song song
        warmup = min(len(items), 2 if autorate else 0)
        for i in range(warmup):
            work(i)
            self.check()
        if fatal:
            raise PipelineError(f"Lỗi TTS: {fatal[0]}")
        if self.rate.cps:
            self.log(f"Tốc độ đọc đo được: {self.rate.cps:.1f} ký tự/giây ở tốc độ 1.0")

        with ThreadPoolExecutor(max_workers=self.tts_workers) as pool:
            futures = [pool.submit(work, i) for i in range(warmup, len(items))]
            for future in futures:
                future.result()
                self.check()
        if fatal:
            raise PipelineError(f"Lỗi TTS: {fatal[0]}")
        if not any(c["audio"] for c in clips):
            raise PipelineError("Không tổng hợp được câu nào, kiểm tra lại TTS key/voice_id.")
        if stats["predicted"] or stats["resynth"]:
            self.log(f"Khớp thời lượng: {stats['predicted']} câu đặt sẵn tốc độ nhanh hơn, "
                     f"{stats['resynth']} câu phải đọc lại bằng binary search "
                     f"({stats['binary']} lượt tìm kiếm)")
        return clips

    def _clone_source_voice(self, client: LolyTTS, items: List[Dict]) -> str:
        """Nhân bản giọng gốc từ đoạn thoại dài nhất (cần account key vc_ak_live_*)."""
        try:
            longest = max(items, key=lambda it: it["end_ms"] - it["start_ms"])
            start = longest["start_ms"]
            end = min(longest["end_ms"], start + 20000)
            ref = self.cache / "voice_ref.wav"
            ffmpeg.cut_audio(self.audio16k, start, end, ref, sample_rate=16000)
            self.log(f"Đang nhân bản giọng gốc từ đoạn {start / 1000:.1f}s - {end / 1000:.1f}s")
            result = client.clone_voice(ref, name=f"pvt-{self.job.id[:8]}")
            self.log(f"Đã tạo voice clone: {result.get('name')} ({result.get('voice_id')})")
            return result.get("voice_id", "")
        except (TtsError, ffmpeg.FFmpegError, ValueError) as exc:
            self.log(f"Nhân bản giọng thất bại, dùng voice mặc định: {exc}", "warn")
            return ""

    def _note_placement(self, line: int, target_ms: int, start_ms: Optional[int],
                        end_ms: Optional[int], drift_ms: Optional[int],
                        budget_ms: Optional[int]) -> None:
        """Ghi mốc chèn thực tế vào báo cáo word-level để UI so với mốc aligner."""
        report = self.word_segments.get(line)
        if report is None:
            return
        report["placed"] = {
            "target": round(target_ms / 1000.0, 3),
            "start": None if start_ms is None else round(start_ms / 1000.0, 3),
            "end": None if end_ms is None else round(end_ms / 1000.0, 3),
            "drift": None if drift_ms is None else round(drift_ms / 1000.0, 3),
            "budget": None if budget_ms is None else round(budget_ms / 1000.0, 3),
            "silent": start_ms is None,
        }

    # 6. Căn chỉnh: đặt từng câu về đúng mốc thời gian gốc, tự bắt lại nhịp khi bị trễ
    def _align(self, clips: List[Dict], items: List[Dict], info: Dict):
        self.stage("Căn chỉnh lồng tiếng với video", 86)
        total_ms = info["duration_ms"]
        budgets = self._budgets(items, total_ms)
        autorate = bool(self.params.get("voice_autorate", True))

        timeline: List[np.ndarray] = []
        cursor_ms = 0
        speeded = 0
        forced = 0
        overruns: List[Tuple[int, int]] = []
        drifts: List[int] = []

        self._debug([
            "",
            "=" * 92,
            "TIMELINE CHÈN LỒNG TIẾNG (mốc đích lấy từ source.srt sau khi đã căn từ)",
            "=" * 92,
        ])

        for index, clip in enumerate(clips):
            self.check()
            item = items[index]
            # start_ms/end_ms bên dưới bị ghi đè bằng vị trí chèn thực tế, nên khung
            # gốc phải được giữ lại: trình chỉnh sửa cần nó làm mốc đối chiếu.
            item.setdefault("orig_start_ms", item["start_ms"])
            item.setdefault("orig_end_ms", item["end_ms"])
            # Câu đầu tiên bắt đầu gần đầu video thì kéo hẳn về 0 cho gọn
            target_start = 0 if (index == 0 and item["start_ms"] < 150) else item["start_ms"]
            first_word = (item.get("source_words") or [{}])[0].get("start")
            # Mốc giọng gốc dứt - trần tuyệt đối cho câu này
            spoken_end = item["end_ms"]

            if not clip["audio"]:
                self._note_placement(item["line"], target_start, None, None, None, None)
                self._debug([f"[dòng {item['line']:>4}] đích {_ts(target_start)} | KHÔNG CÓ AUDIO, bỏ trống"])
                continue

            samples = ffmpeg.decode_pcm(clip["audio"])  # đã được cắt lặng ở bước lồng tiếng
            dub_ms = _duration_ms(samples)
            budget = budgets[index]
            used_file = clip["audio"]

            # Sau khi đã chỉnh speed native mà vẫn tràn thì mới kéo giãn bằng atempo
            if autorate and dub_ms > budget + self.fit_tolerance_ms:
                needed = dub_ms / budget
                # Bình thường chỉ ép tới max_audio_speed cho giọng khỏi méo. Nhưng
                # nếu buông ở đó mà câu vẫn vượt mốc giọng gốc dứt thì ép tiếp tới
                # hard_max_audio_speed - lệch hình khó nghe hơn là giọng nhanh.
                allowed = self.max_audio_speed
                if needed > allowed:
                    allowed = min(needed, self.hard_max_audio_speed)
                target_ms = budget if needed <= allowed else int(dub_ms / allowed)
                fast_file = self.cache / "tts" / f"fast_{index:04d}.wav"
                try:
                    ffmpeg.speed_up_audio(clip["audio"], fast_file, target_ms, dub_ms)
                    samples = ffmpeg.decode_pcm(fast_file)
                    dub_ms = _duration_ms(samples)
                    used_file = str(fast_file)
                    speeded += 1
                    if allowed > self.max_audio_speed:
                        forced += 1
                except ffmpeg.FFmpegError as exc:
                    self.log(f"Dòng {item['line']} tăng tốc lỗi, giữ nguyên: {exc}", "warn")

            # Bản thật sự được ghép vào track (có thể là bản đã atempo): trình chỉnh
            # sửa phải dựng lại từ đúng file này thì mới ra y hệt bản vừa giao.
            clip["final_audio"] = used_file
            clip["final_ms"] = dub_ms

            # Chờ đến đúng mốc gốc nếu đang sớm; nếu đang trễ thì phát ngay để đuổi kịp
            if cursor_ms < target_start:
                timeline.append(_silence(target_start - cursor_ms))
                cursor_ms = target_start
            drift = cursor_ms - target_start
            drifts.append(drift)

            timeline.append(samples)
            item["start_ms"] = cursor_ms
            item["end_ms"] = cursor_ms + dub_ms

            self._note_placement(item["line"], target_start, cursor_ms, cursor_ms + dub_ms, drift, budget)
            overrun = (cursor_ms + dub_ms) - spoken_end
            if overrun > self.max_overrun_ms:
                overruns.append((item["line"], overrun))
                self.log(f"Dòng {item['line']} vượt mốc giọng gốc {overrun / 1000:.2f}s "
                         f"(gốc dứt {_ts(spoken_end)}, lồng tiếng tới {_ts(cursor_ms + dub_ms)})", "warn")
            note = "" if drift <= 0 else f"  << TRỄ {drift / 1000:+.3f}s"
            if overrun > self.max_overrun_ms:
                note += f"  << VƯỢT MỐC GỐC {overrun / 1000:+.3f}s"
            self._debug([
                f"[dòng {item['line']:>4}] đích {_ts(target_start)} | chèn {_ts(cursor_ms)} -> "
                f"{_ts(cursor_ms + dub_ms)} | lệch {drift / 1000:+.3f}s | "
                f"dub {dub_ms / 1000:.3f}s / khung {budget / 1000:.3f}s"
                f" | gốc dứt {_ts(spoken_end)}"
                + (f" | từ đầu tiên của giọng gốc {_ts(first_word * 1000)}" if first_word is not None else "")
                + note
            ])
            cursor_ms += dub_ms

        merged = np.concatenate(timeline) if timeline else _silence(total_ms)
        audio_ms = _duration_ms(merged)
        if audio_ms < total_ms:
            merged = np.concatenate([merged, _silence(total_ms - audio_ms)])
            audio_ms = _duration_ms(merged)

        dub_wav = self.cache / "dubbed.wav"
        ffmpeg.write_wav(merged, dub_wav)

        late = [d for d in drifts if d > 0]
        avg_drift = sum(drifts) / len(drifts) if drifts else 0
        self.log(f"Đã ghép track lồng tiếng: {audio_ms / 1000:.1f}s "
                 f"(gốc {total_ms / 1000:.1f}s, tổng lệch {(audio_ms - total_ms) / 1000:+.1f}s)")
        worst_overrun = max((o for _, o in overruns), default=0)
        self._debug([
            "-" * 92,
            f"Tổng kết: {len(drifts) - len(late)}/{len(drifts)} câu đúng mốc, {len(late)} câu bị trễ, "
            f"lệch trung bình {avg_drift / 1000:.3f}s, lớn nhất {max(drifts or [0]) / 1000:.3f}s, "
            f"{speeded} câu phải atempo ({forced} câu phải ép quá {self.max_audio_speed:.2f}x)",
            f"Vượt mốc giọng gốc quá {self.max_overrun_ms}ms: {len(overruns)}/{len(drifts)} câu"
            + (f", nặng nhất {worst_overrun / 1000:.2f}s ở dòng "
               f"{max(overruns, key=lambda x: x[1])[0]}" if overruns else ""),
        ])
        self.log(f"Đồng bộ: {len(drifts) - len(late)}/{len(drifts)} câu vào đúng mốc gốc, "
                 f"lệch trung bình {avg_drift / 1000:.2f}s, lệch lớn nhất {max(drifts or [0]) / 1000:.2f}s, "
                 f"{speeded} câu phải kéo giãn thêm bằng atempo"
                 + (f" ({forced} câu phải ép quá {self.max_audio_speed:.2f}x)" if forced else ""))
        if overruns:
            self.log(f"{len(overruns)}/{len(drifts)} câu vẫn vượt mốc giọng gốc quá "
                     f"{self.max_overrun_ms}ms, nặng nhất {worst_overrun / 1000:.2f}s", "warn")
        else:
            self.log(f"Không câu nào đọc quá mốc giọng gốc (dung sai {self.max_overrun_ms}ms)")

        self.target_srt.write_text(srt.to_srt(items), encoding="utf-8")
        self.job.set_preview("target_srt", srt.to_srt(items))
        return dub_wav, items

    # 7. Ghép video + lồng tiếng (+ phụ đề)
    def _mix_tracks(self, dubbed: Path) -> Tuple[List[Tuple[Path, float]], List[str], float]:
        """Dựng danh sách track cho bản trộn cuối: nhạc nền + giọng gốc + lồng tiếng.

        Giọng gốc là một đường riêng nhờ stem của Demucs, nên chỉnh được độc lập
        với nhạc nền: để 0 là lồng tiếng thường (thay hẳn giọng), để >0 thì giọng
        gốc phát cùng giọng TTS kiểu thuyết minh chồng tiếng.
        """
        background, default_volume, what, voice_included = self._pick_background()
        bg_volume = _clamp_volume(self.background_volume, default_volume)
        tracks: List[Tuple[Path, float]] = [(background, bg_volume)]
        labels = [f"{what} ×{bg_volume:.2f}"]

        if self.original_voice_volume > 0:
            if voice_included:
                # Nền đang là audio gốc nguyên bản -> giọng gốc đã nằm sẵn trong đó,
                # cộng stem vào nữa là nghe đúp tiếng.
                self.log("Giọng gốc đã nằm sẵn trong nền «audio gốc nguyên bản» nên thanh "
                         "«âm lượng giọng gốc» không áp dụng. Chọn nguồn nền «nhạc nền đã "
                         "tách» nếu muốn chỉnh riêng giọng gốc.", "warn")
            elif self.vocals and self.vocals.is_file():
                tracks.append((self.vocals, self.original_voice_volume))
                labels.append(f"giọng gốc ×{self.original_voice_volume:.2f}")
            else:
                self.log("Chưa tách được stem giọng nên không chỉnh riêng giọng gốc được.", "warn")

        tracks.append((dubbed, self.dubbed_volume))
        labels.append(f"giọng lồng tiếng ×{self.dubbed_volume:.2f}")
        return tracks, labels, bg_volume

    def _warn_if_clipped(self, path: Path, volume: float) -> None:
        """Đếm mẫu chạm trần và cảnh báo, thay vì âm thầm nén tín hiệu.

        Từ khi bỏ chuẩn hoá của amix, âm lượng đúng nghĩa nhưng nền + lồng tiếng
        cùng to thì có thể vượt trần. Nén tự động sẽ phá mất chính cái "giữ nguyên"
        mà người dùng chọn, nên ở đây chỉ báo để họ tự hạ âm lượng.
        """
        try:
            clipped, total = ffmpeg.count_clipped(path)
        except ffmpeg.FFmpegError:
            return
        if not total or not clipped:
            return
        share = clipped / total * 100
        if share < 0.001:
            return
        self.log(f"Có {clipped:,} mẫu ({share:.3f}%) chạm trần biên độ - nghe có thể rè. "
                 f"Hạ âm lượng nền (đang {volume:.2f}) hoặc âm lượng lồng tiếng "
                 f"(đang {self.dubbed_volume:.2f}), hoặc bật separate.mix_limiter.", "warn")

    def _pick_background(self) -> Tuple[Path, float, str, bool]:
        """Chọn file làm tiếng nền, theo lựa chọn của người dùng.

        `original` là đường để giữ nguyên audio gốc: không qua Demucs, chỉ đi qua
        đúng một filter `volume`. Đổi lại giọng gốc vẫn còn trong đó nên sẽ chồng
        lên giọng lồng tiếng - đó là đánh đổi của việc "không biến đổi gì".
        """
        source = Path(self.job.source_path)
        original_volume = float(self.params.get("original_audio_volume", 0.35))
        has_stem = bool(self.accompaniment and self.accompaniment.is_file())

        stem = (self.accompaniment, self.accompaniment_volume,
                "nhạc nền đã tách (không còn giọng gốc)", False)
        if self.background_source == "original":
            return source, original_volume, "audio gốc nguyên bản (chưa qua tách nhạc)", True
        if self.background_source == "accompaniment":
            if has_stem:
                return stem
            self.log("Bạn chọn dùng stem nhạc nền nhưng chưa tách được, quay về audio gốc.", "warn")
            return source, original_volume, "audio gốc nguyên bản (không tách được stem)", True
        if has_stem:
            return stem
        return source, original_volume, "audio gốc nguyên bản (chưa tách được nhạc nền)", True

    def _assemble(self, audio_path: Path, items: List[Dict], info: Dict) -> Dict:
        self.stage("Ghép video hoàn chỉnh", 93)
        stem = Path(self.job.filename).stem
        target_lang = self.params.get("target_lang", "out")
        result: Dict[str, str] = {}

        if not info["has_video"] or not self.novoice:
            out_audio = self.output / f"{stem}-{target_lang}.m4a"
            ffmpeg.audio_only_output(audio_path, out_audio)
            result["audio"] = out_audio.name
        else:
            video_src = self.novoice
            audio_ms = ffmpeg.media_duration_ms(audio_path)
            video_ms = ffmpeg.media_duration_ms(video_src)
            if audio_ms > video_ms + 200:
                self.log(f"Lồng tiếng dài hơn video {(audio_ms - video_ms) / 1000:.1f}s, "
                         f"kéo dài video bằng khung hình cuối")
                extended = self.cache / "novoice_extended.mp4"
                try:
                    ffmpeg.extend_video(video_src, extended, audio_ms - video_ms + 100)
                    video_src = extended
                except ffmpeg.FFmpegError as exc:
                    self.log(f"Kéo dài video thất bại, video sẽ bị cắt theo độ dài gốc: {exc}", "warn")

            out_video = self.output / f"{stem}-{target_lang}.mp4"
            final_audio = audio_path
            if bool(self.params.get("mix_original_audio", True)) and info.get("has_audio"):
                mixed_audio = self.cache / "mixed_audio.wav"
                # Có stem nhạc nền thì trộn stem đó: giữ nguyên nhạc/hiệu ứng mà
                # không kéo theo giọng gốc chồng lên giọng lồng tiếng.
                tracks, labels, bg_volume = self._mix_tracks(audio_path)
                channels = info.get("channels") or 0
                rate = info.get("sample_rate") or 0
                try:
                    ffmpeg.mix_tracks(tracks, mixed_audio, limiter=self.mix_limiter)
                    final_audio = mixed_audio
                    out_ch, out_rate = ffmpeg.audio_layout(mixed_audio)
                    kept = (out_ch == channels and out_rate == rate) if (channels and rate) else False
                    self.log("Đã trộn " + " + ".join(labels) + f" | {out_ch} kênh @ {out_rate}Hz"
                             + (" (đúng như bản gốc)" if kept else
                                f" (bản gốc {channels} kênh @ {rate}Hz)" if channels else ""))
                    self._warn_if_clipped(mixed_audio, bg_volume)
                except ffmpeg.FFmpegError as exc:
                    self.log(f"Trộn nền thất bại, dùng riêng track lồng tiếng: {exc}", "warn")
            elif abs(self.dubbed_volume - 1.0) > 1e-3:
                # Không trộn nền thì mix_audio không chạy, phải chỉnh âm lượng riêng
                louder = self.cache / "dubbed_volume.wav"
                try:
                    ffmpeg.apply_volume(audio_path, louder, self.dubbed_volume)
                    final_audio = louder
                    self.log(f"Đã đặt âm lượng track lồng tiếng {self.dubbed_volume:.2f}")
                except ffmpeg.FFmpegError as exc:
                    self.log(f"Chỉnh âm lượng lồng tiếng thất bại: {exc}", "warn")
            burn = bool(self.params.get("burn_subtitle"))
            soft = bool(self.params.get("soft_subtitle")) and not burn
            try:
                ffmpeg.mux(video_src, final_audio, out_video,
                           subtitle=self.target_srt if (burn or soft) else None,
                           burn_subtitle=burn,
                           copy_video=not burn)
            except ffmpeg.FFmpegError as exc:
                self.log(f"Ghép có phụ đề lỗi ({exc}), ghép lại không phụ đề", "warn")
                ffmpeg.mux(video_src, final_audio, out_video, copy_video=True)
            result["video"] = out_video.name

        dub_out = self.output / f"{stem}-{target_lang}-dubbed.wav"
        shutil.copy2(audio_path, dub_out)
        result["dubbed_audio"] = dub_out.name
        result["source_srt"] = self.source_srt.name
        result["target_srt"] = self.target_srt.name
        if getattr(self, "word_timestamps", None):
            result["word_timestamps"] = self.word_timestamps.name
        if getattr(self, "align_log", None) and self.align_log.exists():
            result["align_log"] = self.align_log.name
        result["lines"] = str(len(items))
        if self.token_usage:
            result["token_usage"] = self.token_usage
        if self.token_cost:
            result["token_cost"] = self.token_cost

        self.stage("Hoàn tất", 100)
        self.log("Xong. Có thể tải kết quả về máy.")
        return result

    # 8. Chụp nguyên liệu cho trình chỉnh sửa (cache bị xoá ngay sau khi job xong)
    def _save_edit_project(self, clips: List[Dict], items: List[Dict], info: Dict) -> None:
        try:
            editor.capture(self, clips, items, info)
            self.log("Đã lưu dữ liệu chỉnh sửa: mở «Chỉnh sửa» để kéo lại mốc hoặc đọc lại từng câu")
        except Exception as exc:  # noqa: BLE001 - hỏng bước này không được làm hỏng job
            self.log(f"Không lưu được dữ liệu chỉnh sửa: {exc}", "warn")


def run_pipeline(job, config: Dict) -> Dict:
    return Pipeline(job, config).run()
