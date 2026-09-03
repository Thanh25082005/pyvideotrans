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

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from . import ffmpeg, srt, vad
from .langs import CJK_LANGS, english_name
from .stt_loli import LoliSTT, SttError
from .translate_openai import OpenAITranslator, TranslateError
from .tts_loly import LolyTTS, TtsError

SR = ffmpeg.SAMPLE_RATE
# Loly 3.5 chỉ nhận speed trong khoảng 0,5-1,5
TTS_SPEED_MIN, TTS_SPEED_MAX = 0.5, 1.5


class PipelineError(RuntimeError):
    pass


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
        self.max_audio_speed = float(self.params.get("max_audio_speed")
                                     or pipe_cfg.get("max_audio_speed", 1.6))
        self.stt_workers = max(1, int(pipe_cfg.get("stt_concurrency", 3)))
        self.tts_workers = max(1, int(pipe_cfg.get("tts_concurrency", 3)))
        # Sai lệch nhỏ hơn ngưỡng này thì bỏ qua, ép tốc độ chỉ làm giọng xấu đi
        self.fit_tolerance_ms = int(pipe_cfg.get("fit_tolerance_ms", 200))
        # Chừa một nhịp thở giữa hai câu liền nhau
        self.gap_reserve_ms = int(pipe_cfg.get("gap_reserve_ms", 120))
        self.rate = RateEstimator()

    # ------------------------------------------------------------------ utils
    def log(self, message: str, level: str = "info") -> None:
        self.job.log(message, level)

    def stage(self, name: str, progress: int) -> None:
        self.job.set_stage(name, progress)

    def check(self) -> None:
        self.job.check_cancel()

    # ------------------------------------------------------------------ stages
    def run(self) -> Dict:
        info = self._prepare()
        segments = self._vad()
        source_items = self._recognize(segments)
        target_items = self._translate(source_items)
        clips = self._dubbing(target_items)
        audio_path, target_items = self._align(clips, target_items, info)
        return self._assemble(audio_path, target_items, info)

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
        ffmpeg.extract_audio_16k(source, self.audio16k)
        self.log("Đã tách audio 16kHz mono để nhận dạng")
        self.check()

        self.novoice = None
        if info["has_video"]:
            self.stage("Tách video không tiếng", 6)
            self.novoice = self.cache / "novoice.mp4"
            ffmpeg.extract_video_only(source, self.novoice, info)
            self.log("Đã tách phần hình ảnh (novoice.mp4)")
        return info

    # 2. VAD: cắt thành các đoạn có tiếng nói
    def _vad(self) -> List:
        self.stage("Cắt câu bằng VAD", 9)
        segments = vad.detect_segments(
            str(self.audio16k),
            min_speech_ms=self.min_speech_ms,
            max_speech_ms=self.max_speech_ms,
            min_silence_ms=self.min_silence_ms,
        )
        if not segments:
            raise PipelineError("Không phát hiện được giọng nói nào trong file.")
        total = sum(e - s for s, e in segments) / 1000.0
        self.log(f"VAD tìm thấy {len(segments)} đoạn thoại, tổng {total:.1f}s")
        return segments

    # 3. Nhận dạng bằng Loli 2.0
    def _recognize(self, segments: List) -> List[Dict]:
        self.stage("Nhận dạng giọng nói (Loli 2.0)", 12)
        stt_cfg = self.config.get("stt", {})
        client = LoliSTT(stt_cfg.get("base_url", ""), stt_cfg.get("api_key", ""))
        language = self.params.get("source_lang") or "auto"
        clip_dir = self.cache / "asr"
        clip_dir.mkdir(exist_ok=True)

        texts: List[str] = [""] * len(segments)
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

        items = [srt.make_item(i + 1, s, e, texts[i]) for i, (s, e) in enumerate(segments)]
        items = srt.clean_and_fix(items)
        if not items:
            raise PipelineError("Nhận dạng không ra nội dung nào. Kiểm tra lại ngôn ngữ nguồn hoặc file audio.")
        join_flag = "" if (self.params.get("source_lang") or "").split("-")[0] in CJK_LANGS else " "
        items = srt.merge_short(items, min_ms=self.min_speech_ms, max_ms=self.max_speech_ms,
                                join_flag=join_flag)

        self.source_srt = self.output / "source.srt"
        self.source_srt.write_text(srt.to_srt(items), encoding="utf-8")
        self.log(f"Phụ đề gốc: {len(items)} dòng -> source.srt")
        self.job.set_preview("source_srt", srt.to_srt(items))
        return items

    # 4. Dịch bằng OpenAI
    def _translate(self, items: List[Dict]) -> List[Dict]:
        target_lang = self.params.get("target_lang") or "vi"
        source_lang = (self.params.get("source_lang") or "auto").split("-")[0]
        self.target_srt = self.output / "target.srt"

        if target_lang == source_lang:
            self.log("Ngôn ngữ đích trùng ngôn ngữ nguồn, bỏ qua bước dịch")
            self.target_srt.write_text(srt.to_srt(items), encoding="utf-8")
            self.job.set_preview("target_srt", srt.to_srt(items))
            return items

        self.stage("Dịch phụ đề (OpenAI)", 45)
        openai_cfg = self.config.get("openai", {})
        translator = OpenAITranslator(
            api_key=openai_cfg.get("api_key", ""),
            base_url=openai_cfg.get("base_url", ""),
            model=openai_cfg.get("model", ""),
            temperature=openai_cfg.get("temperature", 0.3),
            batch_size=openai_cfg.get("batch_size", 20),
        )

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
                source_name=english_name(source_lang) if source_lang != "auto" else "",
                progress=progress,
                budgets=budgets,
            )
        except TranslateError as exc:
            raise PipelineError(f"Lỗi dịch: {exc}") from exc

        translated = srt.align_by_time(items, translated)
        self.target_srt.write_text(srt.to_srt(translated), encoding="utf-8")
        self.log(f"Đã dịch sang {english_name(target_lang)} -> target.srt")
        self.job.set_preview("target_srt", srt.to_srt(translated))
        return translated

    # ----------------------------------------------------------- khung thời gian
    def _budgets(self, items: List[Dict], total_ms: int) -> List[int]:
        """Thời lượng tối đa mỗi câu được phép chiếm: tới lúc câu sau bắt đầu, chừa nhịp thở."""
        budgets = []
        for i, item in enumerate(items):
            next_start = items[i + 1]["start_ms"] if i + 1 < len(items) else max(item["end_ms"], total_ms)
            budgets.append(max(400, next_start - item["start_ms"] - self.gap_reserve_ms))
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
        stats = {"resynth": 0, "predicted": 0}
        step = 1 if len(items) <= 25 else 5
        fatal: List[Exception] = []

        def synth(index: int, speed: float) -> int:
            """Sinh audio, cắt lặng hai đầu, ghi đè lại file và trả về thời lượng thật."""
            item = items[index]
            out_file = clip_dir / f"dub_{index:04d}.wav"
            client.synthesize(item["text"], out_file, language=language,
                              speed=round(speed, 3), dit_steps=dit_steps, fmt="wav")
            samples = _trim_silence(ffmpeg.decode_pcm(out_file))
            ffmpeg.write_wav(samples, out_file)
            duration = _duration_ms(samples)
            self.rate.update(len(item["text"]), duration, speed)
            clips[index]["audio"] = str(out_file)
            clips[index]["dub_ms"] = duration
            clips[index]["speed"] = speed
            return duration

        def work(index: int) -> None:
            if fatal or self.job.cancelled:
                return
            item = items[index]
            budget = budgets[index]
            speed = base_speed
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

                duration = synth(index, speed)

                # Vẫn tràn -> sinh lại đúng một lần với tốc độ đã hiệu chỉnh
                if (resynth_enabled and duration > budget + self.fit_tolerance_ms
                        and speed < TTS_SPEED_MAX - 0.02):
                    corrected = _clamp(speed * duration / budget, speed, TTS_SPEED_MAX)
                    if corrected > speed + 0.04:
                        self.log(f"Dòng {item['line']}: {duration}ms > khung {budget}ms, "
                                 f"đọc lại ở tốc độ {corrected:.2f}")
                        synth(index, corrected)
                        stats["resynth"] += 1
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
                     f"{stats['resynth']} câu phải đọc lại")
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

    # 6. Căn chỉnh: đặt từng câu về đúng mốc thời gian gốc, tự bắt lại nhịp khi bị trễ
    def _align(self, clips: List[Dict], items: List[Dict], info: Dict):
        self.stage("Căn chỉnh lồng tiếng với video", 86)
        total_ms = info["duration_ms"]
        budgets = self._budgets(items, total_ms)
        autorate = bool(self.params.get("voice_autorate", True))

        timeline: List[np.ndarray] = []
        cursor_ms = 0
        speeded = 0
        drifts: List[int] = []

        for index, clip in enumerate(clips):
            self.check()
            item = items[index]
            # Câu đầu tiên bắt đầu gần đầu video thì kéo hẳn về 0 cho gọn
            target_start = 0 if (index == 0 and item["start_ms"] < 150) else item["start_ms"]

            if not clip["audio"]:
                continue

            samples = ffmpeg.decode_pcm(clip["audio"])  # đã được cắt lặng ở bước lồng tiếng
            dub_ms = _duration_ms(samples)
            budget = budgets[index]

            # Sau khi đã chỉnh speed native mà vẫn tràn thì mới kéo giãn bằng atempo
            if autorate and dub_ms > budget + self.fit_tolerance_ms:
                ratio = dub_ms / budget
                target_ms = budget if ratio <= self.max_audio_speed else int(dub_ms / self.max_audio_speed)
                fast_file = self.cache / "tts" / f"fast_{index:04d}.wav"
                try:
                    ffmpeg.speed_up_audio(clip["audio"], fast_file, target_ms, dub_ms)
                    samples = ffmpeg.decode_pcm(fast_file)
                    dub_ms = _duration_ms(samples)
                    speeded += 1
                except ffmpeg.FFmpegError as exc:
                    self.log(f"Dòng {item['line']} tăng tốc lỗi, giữ nguyên: {exc}", "warn")

            # Chờ đến đúng mốc gốc nếu đang sớm; nếu đang trễ thì phát ngay để đuổi kịp
            if cursor_ms < target_start:
                timeline.append(_silence(target_start - cursor_ms))
                cursor_ms = target_start
            drifts.append(cursor_ms - target_start)

            timeline.append(samples)
            item["start_ms"] = cursor_ms
            item["end_ms"] = cursor_ms + dub_ms
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
        self.log(f"Đồng bộ: {len(drifts) - len(late)}/{len(drifts)} câu vào đúng mốc gốc, "
                 f"lệch trung bình {avg_drift / 1000:.2f}s, lệch lớn nhất {max(drifts or [0]) / 1000:.2f}s, "
                 f"{speeded} câu phải kéo giãn thêm bằng atempo")

        self.target_srt.write_text(srt.to_srt(items), encoding="utf-8")
        self.job.set_preview("target_srt", srt.to_srt(items))
        return dub_wav, items

    # 7. Ghép video + lồng tiếng (+ phụ đề)
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
            burn = bool(self.params.get("burn_subtitle"))
            soft = bool(self.params.get("soft_subtitle")) and not burn
            try:
                ffmpeg.mux(video_src, audio_path, out_video,
                           subtitle=self.target_srt if (burn or soft) else None,
                           burn_subtitle=burn,
                           copy_video=not burn)
            except ffmpeg.FFmpegError as exc:
                self.log(f"Ghép có phụ đề lỗi ({exc}), ghép lại không phụ đề", "warn")
                ffmpeg.mux(video_src, audio_path, out_video, copy_video=True)
            result["video"] = out_video.name

        dub_out = self.output / f"{stem}-{target_lang}-dubbed.wav"
        shutil.copy2(audio_path, dub_out)
        result["dubbed_audio"] = dub_out.name
        result["source_srt"] = self.source_srt.name
        result["target_srt"] = self.target_srt.name
        result["lines"] = str(len(items))

        self.stage("Hoàn tất", 100)
        self.log("Xong. Có thể tải kết quả về máy.")
        return result


def run_pipeline(job, config: Dict) -> Dict:
    return Pipeline(job, config).run()
