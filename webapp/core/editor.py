"""Trình chỉnh sửa sau lồng tiếng: mô hình project, đọc lại từng câu, render lại.

Pipeline chạy xong sẽ chụp toàn bộ nguyên liệu cần cho việc sửa tay vào
`data/jobs/<id>/edit/` (clip TTS từng câu, stem nhạc nền/giọng gốc, video không
tiếng, đường bao sóng) rồi ghi `edit/project.json`. Từ đó trở đi editor chỉ làm
việc trên project.json nên restart server vẫn sửa tiếp được - khác với
JobManager vốn chỉ sống trong RAM.

Khác biệt quan trọng so với `Pipeline._align`: ở đây mỗi câu được đặt tại mốc
tuyệt đối của chính nó (overlay vào một buffer dài bằng video) thay vì nối tiếp
theo con trỏ. Nhờ vậy kéo một câu KHÔNG đẩy các câu phía sau - đó là điều kiện
tiên quyết để sửa tay được.

Module này không import pipeline (pipeline import ngược lại nó).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import ffmpeg, srt, vad
from .langs import english_name
from .settings import JOBS_DIR, job_dir, load_config
from .stt_loli import LoliSTT, SttError
from .translate_openai import OpenAITranslator, TranslateError
from .tts_loly import LolyTTS, TtsError

SR = ffmpeg.SAMPLE_RATE
PROJECT_VERSION = 1
# Độ phân giải đường bao sóng: đủ mịn để zoom sát mà JSON vẫn nhẹ
PEAKS_PER_SECOND = 50
PEAKS_MAX = 120_000
CLIP_PEAKS_PER_SECOND = 30
CLIP_PEAKS_MAX = 400
# Lệch dưới ngưỡng này thì không ép atempo, kéo giãn chỉ làm giọng xấu đi
FIT_TOLERANCE_MS = 60
TTS_SPEED_MIN, TTS_SPEED_MAX = 0.5, 1.5
# Ngân sách thời lượng khi dịch lại một đoạn - giữ giống pipeline._budgets để câu
# dịch mới vẫn lọt đúng cái lỗ mà câu cũ đang ngồi
GAP_RESERVE_MS = 120
MAX_OVERRUN_MS = 500
# Dò thoại sót trong khoảng lặng: lỗ ngắn hơn thế này thì không bõ gọi ASR
MIN_GAP_MS = 700
# Tham số VAD nới rộng cho lần dò lại. Chạy lại đúng tham số cũ thì cũng vứt đi
# y như lần đầu, nên phải nới hai chỗ đã loại nó: ngưỡng độ dài tối thiểu
# (min_speech_ms 1200 -> 400, câu chen một hai tiếng mới lọt) và bộ lọc nhạc.
RESCAN_VAD = {"min_speech_ms": 400, "max_speech_ms": 18000, "min_silence_ms": 300,
              "music_filter": False, "subtract_floor": True}


class EditorError(RuntimeError):
    pass


# --------------------------------------------------------------- đường dẫn
def edit_dir(job_id: str) -> Path:
    path = job_dir(job_id) / "edit"
    (path / "clips").mkdir(parents=True, exist_ok=True)
    (path / "peaks").mkdir(parents=True, exist_ok=True)
    return path


def project_path(job_id: str) -> Path:
    return job_dir(job_id) / "edit" / "project.json"


def resolve(job_id: str, rel: str) -> Path:
    """Đường dẫn tuyệt đối của một file trong job, chặn đường dẫn thoát ra ngoài."""
    root = (JOBS_DIR / job_id).resolve()
    path = (root / str(rel or "")).resolve()
    if not str(path).startswith(str(root)) or not path.is_file():
        raise EditorError(f"Không tìm thấy file: {rel}")
    return path


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


# ------------------------------------------------------------ đọc/ghi project
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(job_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(job_id, threading.Lock())


def load(job_id: str) -> Dict:
    path = project_path(job_id)
    if not path.is_file():
        raise EditorError("Job này chưa có dữ liệu chỉnh sửa (chỉ job chạy xong mới có)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise EditorError(f"project.json hỏng: {exc}") from exc


def save(job_id: str, project: Dict) -> Dict:
    edit_dir(job_id)
    path = project_path(job_id)
    project["updated_at"] = time.time()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return project


def list_projects() -> List[Dict]:
    """Danh sách project đọc thẳng từ đĩa - không phụ thuộc JobManager trong RAM."""
    out: List[Dict] = []
    if not JOBS_DIR.is_dir():
        return out
    for path in JOBS_DIR.glob("*/edit/project.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "job_id": data.get("job_id", path.parent.parent.name),
            "filename": data.get("filename", ""),
            "duration_ms": data.get("duration_ms", 0),
            "segments": len(data.get("segments", [])),
            "target_lang": data.get("target_lang", ""),
            "created_at": data.get("created_at", 0),
            "updated_at": data.get("updated_at", 0),
            "rendered": bool((data.get("output") or {}).get("name")),
        })
    out.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    return out


def delete(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id) / "edit", ignore_errors=True)


# ------------------------------------------------------------------ audio
def _duration_ms(samples: np.ndarray) -> int:
    return int(samples.size * 1000 / SR)


def _silence_trimmed(samples: np.ndarray, floor_db: float = -45.0, margin_ms: int = 30) -> np.ndarray:
    """Cắt lặng hai đầu clip TTS - bản sao logic của pipeline để lồng tiếng bám đúng mốc."""
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
    return samples[max(0, loud[0] * win - margin): min(samples.size, (loud[-1] + 1) * win + margin)]


def peaks_of(samples: np.ndarray, buckets: int) -> List[float]:
    """Biên độ lớn nhất của từng ô thời gian, chuẩn hoá về 0..1."""
    if samples.size == 0 or buckets <= 0:
        return []
    count = int(min(buckets, samples.size))
    edges = np.linspace(0, samples.size, count + 1).astype(np.int64)[:-1]
    values = np.maximum.reduceat(np.abs(samples.astype(np.int32)), edges)
    return [round(float(v) / 32768.0, 3) for v in values]


def peaks_of_file(path: Path, per_second: int = PEAKS_PER_SECOND, cap: int = PEAKS_MAX) -> Dict:
    samples = ffmpeg.decode_pcm(path)
    duration = _duration_ms(samples)
    buckets = int(min(cap, max(1, duration / 1000 * per_second)))
    return {"duration_ms": duration, "peaks": peaks_of(samples, buckets)}


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ------------------------------------------------------- chụp project từ pipeline
def _adopt(src: Optional[Path], edit: Path, name: str) -> Optional[Path]:
    """Chuyển một file từ cache/ sang edit/ - cache bị xoá ngay khi job xong."""
    if not src:
        return None
    src = Path(src)
    if not src.is_file():
        return None
    dest = edit / name
    try:
        shutil.move(str(src), dest)
    except OSError:
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None
    return dest


def _adopt_audio(src: Optional[Path], edit: Path, stem: str) -> Optional[Path]:
    """Như _adopt nhưng nén sang FLAC.

    Hai stem của Demucs là thứ nặng nhất phải giữ lại để còn trộn lại được; FLAC
    không mất mát mà chỉ tốn chừng một nửa chỗ so với wav.
    """
    if not src or not Path(src).is_file():
        return None
    dest = edit / f"{stem}.flac"
    try:
        ffmpeg.run_ffmpeg(["-y", "-i", str(src), "-c:a", "flac", "-compression_level", "5", str(dest)])
    except ffmpeg.FFmpegError:
        return _adopt(src, edit, f"{stem}.wav")
    Path(src).unlink(missing_ok=True)
    return dest


def capture(pipe, clips: List[Dict], items: List[Dict], info: Dict) -> Dict:
    """Ghi lại mọi thứ cần cho editor. Gọi ở cuối Pipeline.run(), TRƯỚC khi cache bị xoá.

    `pipe` là đối tượng Pipeline (dùng theo kiểu duck typing để tránh import vòng).
    """
    job = pipe.job
    root = Path(job.dir)
    edit = edit_dir(job.id)
    clips_dir = edit / "clips"
    source = Path(job.source_path)

    media: Dict[str, str] = {}
    novoice = _adopt(getattr(pipe, "novoice", None), edit, "novoice.mp4")
    if novoice:
        media["novoice"] = _relative(novoice, root)
    background = _adopt_audio(getattr(pipe, "accompaniment", None), edit, "accompaniment")
    if background:
        media["background"] = _relative(background, root)
    vocals = _adopt_audio(getattr(pipe, "vocals", None), edit, "vocals")
    if vocals:
        media["vocals"] = _relative(vocals, root)
    if source.is_file():
        media["source"] = _relative(source, root)
        # Bản aac nhẹ để trình duyệt phát được audio gốc kể cả khi container gốc lạ
        original = edit / "original.m4a"
        try:
            ffmpeg.audio_only_output(source, original)
            media["original_audio"] = _relative(original, root)
        except ffmpeg.FFmpegError:
            pass

    segments: List[Dict] = []
    clip_peaks: Dict[str, List[float]] = {}
    for index, item in enumerate(items):
        clip = clips[index] if index < len(clips) else {}
        seg_id = index + 1
        rel_clip, clip_ms = "", 0
        audio = clip.get("final_audio") or clip.get("audio")
        if audio and Path(audio).is_file():
            dest = clips_dir / f"seg{seg_id:04d}.wav"
            try:
                shutil.copy2(audio, dest)
                samples = ffmpeg.decode_pcm(dest)
                clip_ms = _duration_ms(samples)
                rel_clip = f"edit/clips/{dest.name}"
                clip_peaks[str(seg_id)] = peaks_of(
                    samples, int(min(CLIP_PEAKS_MAX, max(8, clip_ms / 1000 * CLIP_PEAKS_PER_SECOND))))
            except (OSError, ffmpeg.FFmpegError):
                rel_clip, clip_ms = "", 0
        start_ms = int(item.get("start_ms", 0))
        segments.append({
            "id": seg_id,
            "line": int(item.get("line", seg_id)),
            # Mốc chèn thực tế của câu lồng tiếng (đây là cái người dùng kéo)
            "start_ms": start_ms,
            # Khung của giọng gốc: mốc tham chiếu, chỉ để nhìn và để "về đúng mốc"
            "orig_start_ms": int(item.get("orig_start_ms", start_ms)),
            "orig_end_ms": int(item.get("orig_end_ms", item.get("end_ms", start_ms))),
            "source_text": str(item.get("source_text") or ""),
            "target_text": str(item.get("text") or ""),
            "clip": rel_clip,
            "clip_ms": clip_ms,
            # Độ dài mong muốn; khác clip_ms thì lúc render sẽ ép bằng atempo
            "fit_ms": 0,
            "gain": 1.0,
            "muted": False,
            "speed": round(float(clip.get("speed") or 1.0), 3),
        })

    _write_json(edit / "peaks" / "clips.json", clip_peaks)
    try:
        _write_json(edit / "peaks" / "original.json", peaks_of_file(source))
    except (ffmpeg.FFmpegError, OSError) as exc:
        pipe.log(f"Không dựng được đường bao sóng của audio gốc: {exc}", "warn")

    params = dict(getattr(job, "params", {}) or {})
    try:
        _, default_bg_volume, _, _ = pipe._pick_background()
    except Exception:  # noqa: BLE001 - chỉ để lấy số mặc định, hỏng thì lấy 0.9
        default_bg_volume = 0.9
    raw_bg = pipe.background_volume
    project = {
        "version": PROJECT_VERSION,
        "job_id": job.id,
        "filename": job.filename,
        "target_lang": params.get("target_lang", ""),
        "source_lang": params.get("source_lang", ""),
        "created_at": time.time(),
        "duration_ms": int(info.get("duration_ms", 0)),
        "has_video": bool(info.get("has_video")),
        "media": media,
        "mix": {
            "background_source": pipe.background_source,
            "background_volume": round(float(default_bg_volume if raw_bg in (None, "") else raw_bg), 3),
            "original_voice_volume": round(float(pipe.original_voice_volume), 3),
            "dubbed_volume": round(float(pipe.dubbed_volume), 3),
            "mix_original_audio": bool(params.get("mix_original_audio", True)),
            "limiter": bool(pipe.mix_limiter),
            "burn_subtitle": bool(params.get("burn_subtitle")),
            "soft_subtitle": bool(params.get("soft_subtitle")),
        },
        "translate": {
            # Chỉ thị dịch riêng của job phải giữ lại: lúc dịch lại một đoạn trong
            # editor mà thiếu nó thì giọng văn/xưng hô sẽ lệch với phần còn lại.
            "instruction": str(params.get("instruction") or ""),
        },
        "tts": {
            "voice_id": params.get("voice_id", ""),
            "language": params.get("target_lang", "auto"),
            "speed": round(float(params.get("speed") or 1.0), 3),
            "dit_steps": int(params.get("dit_steps") or 16),
        },
        "segments": segments,
        "output": {},
    }
    save(job.id, project)
    return project


# --------------------------------------------------------------- sửa project
EDITABLE_SEGMENT_FIELDS = ("start_ms", "fit_ms", "gain", "muted", "target_text")


def _segment(project: Dict, seg_id: int) -> Dict:
    for seg in project.get("segments", []):
        if int(seg.get("id")) == int(seg_id):
            return seg
    raise EditorError(f"Không có câu số {seg_id}")


def _sanitize_segment(patch: Dict) -> Dict:
    """Chỉ nhận các trường người dùng được phép sửa - clip/clip_ms do server giữ."""
    out: Dict = {}
    if "start_ms" in patch:
        out["start_ms"] = max(0, int(patch["start_ms"]))
    if "fit_ms" in patch:
        out["fit_ms"] = max(0, min(int(patch["fit_ms"] or 0), 120_000))
    if "gain" in patch:
        out["gain"] = max(0.0, min(float(patch["gain"]), 4.0))
    if "muted" in patch:
        out["muted"] = bool(patch["muted"])
    if "target_text" in patch:
        out["target_text"] = str(patch["target_text"])[:2000]
    return out


def update(job_id: str, payload: Dict) -> Dict:
    """Ghi các thay đổi từ UI: cấu hình trộn, tham số TTS và các câu."""
    with _lock_for(job_id):
        project = load(job_id)
        mix = payload.get("mix")
        if isinstance(mix, dict):
            for key in ("background_volume", "original_voice_volume", "dubbed_volume"):
                if key in mix:
                    project["mix"][key] = max(0.0, min(float(mix[key]), 4.0))
            for key in ("mix_original_audio", "limiter", "burn_subtitle", "soft_subtitle"):
                if key in mix:
                    project["mix"][key] = bool(mix[key])
            if mix.get("background_source") in ("auto", "original", "accompaniment"):
                project["mix"]["background_source"] = mix["background_source"]
        translate = payload.get("translate")
        if isinstance(translate, dict) and "instruction" in translate:
            # Chỉ thị dịch dùng chung cho mọi box khi gen lại một đoạn
            project.setdefault("translate", {})["instruction"] = str(translate["instruction"])[:4000]
        tts = payload.get("tts")
        if isinstance(tts, dict):
            if "voice_id" in tts:
                project["tts"]["voice_id"] = str(tts["voice_id"]).strip()
            if "speed" in tts:
                project["tts"]["speed"] = max(TTS_SPEED_MIN, min(float(tts["speed"]), TTS_SPEED_MAX))
            if "dit_steps" in tts:
                project["tts"]["dit_steps"] = max(1, min(int(tts["dit_steps"]), 64))
        segments = payload.get("segments")
        if isinstance(segments, list):
            by_id = {int(s["id"]): s for s in project["segments"]}
            for patch in segments:
                seg = by_id.get(int(patch.get("id", -1)))
                if seg:
                    seg.update(_sanitize_segment(patch))
        return save(job_id, project)


def _clip_peaks_path(job_id: str) -> Path:
    return edit_dir(job_id) / "peaks" / "clips.json"


def _store_clip_peaks(job_id: str, updates: Dict[int, List[float]]) -> None:
    """Ghi đường bao sóng của các clip vừa sinh. Nhận cả lô để gen lại một đoạn
    dài không phải đọc/ghi lại clips.json từng câu một."""
    path = _clip_peaks_path(job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    data.update({str(key): values for key, values in updates.items()})
    _write_json(path, data)


def _tts_client(project: Dict, voice_id: Optional[str] = None) -> LolyTTS:
    cfg = load_config().get("tts", {})
    voice = (voice_id or project["tts"].get("voice_id") or cfg.get("voice_id") or "").strip()
    client = LolyTTS(cfg.get("base_url", ""), cfg.get("api_key", ""), voice)
    if not client.api_key:
        raise EditorError("Chưa cấu hình TTS API key")
    return client


def _fit_duration(seg: Dict, fit_ms: Optional[int] = None) -> Tuple[int, Optional[float]]:
    """(độ dài đặt hàng, tham số duration gửi Loly).

    "Vừa khung" dùng tham số duration của Loly: chính xác hơn nhiều so với đọc
    xong rồi kéo atempo. Ngoài khoảng model nhận thì trả None - cứ đọc tự nhiên.
    """
    target_ms = int(fit_ms or seg.get("fit_ms") or 0)
    if target_ms <= 0:
        target_ms = max(0, int(seg.get("orig_end_ms", 0)) - int(seg.get("orig_start_ms", 0)))
    duration = round(target_ms / 1000.0, 3) if 500 <= target_ms <= 30_000 else None
    return target_ms, duration


def _synthesize(job_id: str, project: Dict, seg: Dict, client: LolyTTS, text: str,
                speed: float, steps: int, duration: Optional[float]) -> Dict:
    """Đọc lại một câu và ghi clip mới vào edit/clips/. Chưa đụng tới project.json.

    Trả về {"patch": các trường cần đè lên câu, "peaks": đường bao sóng clip mới}.
    """
    version = int(seg.get("version", 1)) + 1
    out_file = edit_dir(job_id) / "clips" / f"seg{int(seg['id']):04d}_v{version}.wav"
    try:
        client.synthesize(text, out_file, language=project["tts"].get("language", "auto"),
                          speed=round(speed, 3), dit_steps=steps, fmt="wav", duration=duration)
        samples = _silence_trimmed(ffmpeg.decode_pcm(out_file))
        ffmpeg.write_wav(samples, out_file)
    except (TtsError, ffmpeg.FFmpegError) as exc:
        out_file.unlink(missing_ok=True)
        raise EditorError(f"Đọc lại thất bại: {exc}") from exc

    clip_ms = _duration_ms(samples)
    patch = {
        "clip": f"edit/clips/{out_file.name}",
        "clip_ms": clip_ms,
        "target_text": text,
        "speed": round(speed, 3),
        "version": version,
    }
    if duration:
        # Đã đặt hàng đúng độ dài rồi thì không cần atempo ép thêm nữa
        patch["fit_ms"] = 0
    return {"patch": patch, "peaks": peaks_of(
        samples, int(min(CLIP_PEAKS_MAX, max(8, clip_ms / 1000 * CLIP_PEAKS_PER_SECOND))))}


def regenerate(job_id: str, seg_id: int, text: Optional[str] = None,
               speed: Optional[float] = None, fit: bool = False,
               fit_ms: Optional[int] = None, voice_id: Optional[str] = None,
               dit_steps: Optional[int] = None) -> Dict:
    """Đọc lại đúng một câu. Chỉ tốn một request TTS, không đụng tới ASR/dịch."""
    project = load(job_id)
    seg = _segment(project, seg_id)
    client = _tts_client(project, voice_id)

    content = (text if text is not None else seg.get("target_text", "")).strip()
    if not content:
        raise EditorError("Câu này chưa có text để đọc")
    speed = float(speed if speed is not None else project["tts"].get("speed", 1.0))
    speed = max(TTS_SPEED_MIN, min(speed, TTS_SPEED_MAX))
    steps = int(dit_steps if dit_steps is not None else project["tts"].get("dit_steps", 16))
    steps = max(1, min(steps, 64))
    target_ms, duration = _fit_duration(seg, fit_ms) if fit else (0, None)

    result = _synthesize(job_id, project, seg, client, content, speed, steps, duration)
    with _lock_for(job_id):
        project = load(job_id)
        seg = _segment(project, seg_id)
        seg.update(result["patch"])
        save(job_id, project)
        _store_clip_peaks(job_id, {seg_id: result["peaks"]})
    return {"segment": seg, "peaks": result["peaks"], "requested_ms": target_ms}


# ------------------------------------------------------ gen lại nguyên một đoạn
def segments_in_range(project: Dict, start_ms: int, end_ms: int) -> List[Dict]:
    """Các câu có box chạm vào [start_ms, end_ms) - đúng cái người dùng khoanh trên timeline."""
    picked: List[Dict] = []
    for seg in project.get("segments", []):
        begin = int(seg.get("start_ms", 0))
        finish = begin + max(effective_ms(seg), 1)
        if not seg.get("clip"):
            # Câu mất clip thì lấy khung giọng gốc làm hộp, không thì khoanh không trúng
            begin = min(begin, int(seg.get("orig_start_ms", begin)))
            finish = max(finish, int(seg.get("orig_end_ms", finish)))
        if finish > start_ms and begin < end_ms:
            picked.append(seg)
    return picked


def segments_in_ranges(project: Dict, ranges: List[Tuple[int, int]]) -> List[Dict]:
    """Hợp của nhiều đoạn, mỗi câu chỉ lấy một lần dù nằm trong hai box chồng nhau."""
    seen: set = set()
    picked: List[Dict] = []
    for start_ms, end_ms in ranges:
        for seg in segments_in_range(project, start_ms, end_ms):
            if int(seg["id"]) not in seen:
                seen.add(int(seg["id"]))
                picked.append(seg)
    picked.sort(key=lambda seg: int(seg["id"]))
    return picked


def _budget_ms(project: Dict, seg: Dict) -> int:
    """Số ms câu này được phép chiếm, để nói thẳng cho LLM biết dịch dài bao nhiêu là vừa.

    Cùng công thức với `Pipeline._budgets`: lấy cái chặt hơn giữa "tới lúc câu
    sau bắt đầu" và "không đọc quá lúc giọng gốc đã dứt".
    """
    segs = project.get("segments", [])
    index = next((i for i, s in enumerate(segs) if int(s["id"]) == int(seg["id"])), -1)
    spoken = int(seg.get("orig_end_ms", 0)) - int(seg.get("orig_start_ms", 0)) + MAX_OVERRUN_MS
    if 0 <= index < len(segs) - 1:
        room = int(segs[index + 1].get("start_ms", 0)) - int(seg.get("start_ms", 0)) - GAP_RESERVE_MS
        return max(400, min(room, spoken))
    return max(400, spoken)


def _as_item(seg: Dict) -> Dict:
    """Câu trong project -> item cho translator. Dùng id làm số dòng vì id chắc chắn không trùng."""
    return {"line": int(seg["id"]), "text": (seg.get("source_text") or "").strip(),
            "start_ms": int(seg.get("orig_start_ms", 0)), "end_ms": int(seg.get("orig_end_ms", 0))}


def _renumber(project: Dict) -> None:
    """Xếp câu theo thời gian rồi đánh lại số `line`.

    `id` giữ nguyên vì tên file clip và khoá đường bao sóng bám theo nó; `line`
    chỉ là số hiển thị nên đánh lại thoải mái.
    """
    segments = project.setdefault("segments", [])
    segments.sort(key=lambda seg: (int(seg.get("start_ms", 0)), int(seg["id"])))
    for order, seg in enumerate(segments, 1):
        seg["line"] = order


def add_segment(job_id: str, start_ms: int, end_ms: int, text: str,
                source_text: str = "") -> Dict:
    """Chèn một câu do người dùng tự gõ lời, đọc luôn bằng TTS.

    Câu này cố tình để `source_text` rỗng: bước «Dịch lại» chỉ đụng vào câu có
    transcript gốc, nên lời bạn tự gõ không bao giờ bị dịch đè lên.

    Không ép đọc vừa khung: khung ở đây do bạn kéo tay chứ không phải mốc giọng
    gốc, ép cho vừa chỉ làm giọng méo.
    """
    text = (text or "").strip()
    if not text:
        raise EditorError("Chưa nhập lời cho câu mới")
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 200, int(end_ms))

    with _lock_for(job_id):
        project = load(job_id)
        client = _tts_client(project)
        speed = max(TTS_SPEED_MIN, min(float(project["tts"].get("speed", 1.0)), TTS_SPEED_MAX))
        steps = max(1, min(int(project["tts"].get("dit_steps", 16)), 64))
        seg = {
            "id": max((int(s["id"]) for s in project.get("segments", [])), default=0) + 1,
            "line": 0,
            "start_ms": start_ms,
            "orig_start_ms": start_ms,
            "orig_end_ms": end_ms,
            "source_text": str(source_text or ""),
            "target_text": text,
            "clip": "", "clip_ms": 0, "fit_ms": 0,
            "gain": 1.0, "muted": False, "speed": speed,
            "manual": True,
        }
        result = _synthesize(job_id, project, seg, client, text, speed, steps, duration=None)
        seg.update(result["patch"])
        project.setdefault("segments", []).append(seg)
        _renumber(project)
        save(job_id, project)
        _store_clip_peaks(job_id, {seg["id"]: result["peaks"]})
    return {"segment": seg, "peaks": result["peaks"]}


# ------------------------------- dò lại thoại bị bỏ sót trong khoảng lặng
def _occupied(seg: Dict) -> Tuple[int, int]:
    """Khoảng thời gian một câu đang chiếm, tính cả khung giọng gốc lẫn box lồng tiếng."""
    begin = min(int(seg.get("start_ms", 0)), int(seg.get("orig_start_ms", 0)))
    end = max(int(seg.get("start_ms", 0)) + effective_ms(seg), int(seg.get("orig_end_ms", 0)))
    return begin, max(end, begin)


def gaps_in_ranges(project: Dict, ranges: List[Tuple[int, int]],
                   min_ms: int = MIN_GAP_MS) -> List[Tuple[int, int]]:
    """Phần của các box chưa câu nào chiếm - đúng những chỗ đáng đem đi ASR lại."""
    taken = sorted(_occupied(seg) for seg in project.get("segments", []))
    gaps: List[Tuple[int, int]] = []
    for start_ms, end_ms in sorted(ranges):
        cursor = start_ms
        for begin, end in taken:
            if end <= cursor:
                continue
            if begin >= end_ms:
                break
            if begin - cursor >= min_ms:
                gaps.append((cursor, begin))
            cursor = max(cursor, end)
        if end_ms - cursor >= min_ms:
            gaps.append((cursor, end_ms))
    return gaps


def _asr_source(job_id: str, project: Dict) -> Optional[Path]:
    """Ưu tiên stem giọng đã tách - đó chính là thứ pipeline đưa cho ASR lần đầu."""
    media = project.get("media", {})
    for key in ("vocals", "source"):
        if media.get(key):
            try:
                return resolve(job_id, media[key])
            except EditorError:
                continue
    return None


def _rescan(job_id: str, project: Dict, ranges: List[Tuple[int, int]],
            log: Callable[[str], None], stage: Callable[[str, int], None],
            base: int, span: int) -> List[Dict]:
    """Chạy VAD + ASR trên những chỗ trống trong box, trả về các câu mới tìm được.

    Chỉ đụng vào phần chưa có câu nào: câu sẵn có giữ nguyên transcript và mốc,
    nên dò lại không bao giờ phá thứ đang đúng.
    """
    gaps = gaps_in_ranges(project, ranges)
    if not gaps:
        log("Trong box không còn chỗ trống nào để dò lại")
        return []
    source = _asr_source(job_id, project)
    if not source:
        log("Không còn file audio gốc của job này nên không dò lại được")
        return []

    stt_cfg = load_config().get("stt", {})
    client = LoliSTT(stt_cfg.get("base_url", ""), stt_cfg.get("api_key", ""))
    if not client.api_key:
        raise EditorError("Chưa cấu hình STT API key nên không nhận dạng lại được")
    language = (project.get("source_lang") or "auto")
    total_gap = sum(b - a for a, b in gaps)
    log(f"Dò lại {len(gaps)} chỗ trống, tổng {total_gap / 1000:.1f}s "
        f"(VAD nới rộng, tắt bộ lọc nhạc) trên {source.name}")

    tmp = edit_dir(job_id) / "tmp"
    tmp.mkdir(exist_ok=True)
    found: List[Dict] = []
    try:
        for index, (start_ms, end_ms) in enumerate(gaps, 1):
            stage("Nhận dạng lại đoạn trống", base + int(span * index / max(1, len(gaps))))
            chunk = tmp / f"gap_{index:03d}.wav"
            try:
                ffmpeg.cut_audio(source, start_ms, end_ms, chunk)
                pieces = vad.analyze(str(chunk), **RESCAN_VAD)["segments"]
            except (ffmpeg.FFmpegError, OSError) as exc:
                log(f"Chỗ trống {index}: cắt/VAD lỗi, bỏ qua ({exc})")
                continue
            if not pieces:
                continue
            for order, (piece_start, piece_end) in enumerate(pieces):
                clip = tmp / f"gap_{index:03d}_{order:02d}.wav"
                try:
                    ffmpeg.cut_audio(chunk, piece_start, piece_end, clip)
                    text = (client.transcribe(clip, language=language).get("text") or "").strip()
                except (SttError, ffmpeg.FFmpegError) as exc:
                    log(f"Chỗ trống {index} đoạn {order + 1}: nhận dạng lỗi, bỏ qua ({exc})")
                    continue
                if text:
                    found.append(srt.make_item(len(found) + 1, start_ms + piece_start,
                                               start_ms + piece_end, text))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not found:
        log("Dò lại xong: không có thoại nào trong các chỗ trống")
        return []
    found = _plausible(project, srt.clean_and_fix(found), log)
    if not found:
        log("Dò lại xong: những gì nhận được đều là ASR bịa từ nhạc/tiếng động")
        return []
    log(f"Dò lại xong: thêm {len(found)} câu mới")
    return found


def _plausible(project: Dict, found: List[Dict], log: Callable[[str], None]) -> List[Dict]:
    """Loại các dòng ASR bịa ra, chấm dựa trên transcript sẵn có của cả video.

    Nới VAD ra thì mấy tiếng động ngắn cũng lọt vào ASR, và Loly không trả về rỗng
    mà đoán bừa - điển hình là một chữ Hán lạc vào video tiếng Anh. Không thể đem
    các dòng mới ra tự chấm lẫn nhau: một dòng thì nó tự làm "đa số" cho chính nó.
    Phải đặt cạnh 80 dòng transcript kia thì cái lạc loài mới lộ ra.
    """
    known = [srt.make_item(0, int(seg.get("orig_start_ms", 0)), int(seg.get("orig_end_ms", 0)),
                           seg.get("source_text") or "")
             for seg in project.get("segments", []) if (seg.get("source_text") or "").strip()]
    if not known:
        return found

    script = srt._script_of(" ".join(item["text"] for item in known))
    kept: List[Dict] = []
    for item in found:
        if srt._script_of(item["text"]) != script:
            log(f"Bỏ dòng dò được {item['text']!r}: khác hệ chữ với phần còn lại của video")
            continue
        kept.append(item)
    if not kept:
        return []

    # Trộn vào transcript thật rồi mới lọc, để bộ lọc có đúng ngữ cảnh mà so
    merged = sorted(known + kept, key=lambda item: item["start_ms"])
    survived = {id(item) for item in srt.drop_hallucinations(merged)[0]}
    out = [item for item in kept if id(item) in survived]
    if len(out) < len(kept):
        log(f"Bỏ thêm {len(kept) - len(out)} dòng bị chấm là ASR bịa ra")
    return out


def _insert_discovered(project: Dict, found: List[Dict]) -> List[Dict]:
    """Chèn câu mới vào project rồi xếp lại theo thời gian và đánh số `line`.

    `id` của câu cũ giữ nguyên vì tên file clip và khoá đường bao sóng bám theo nó;
    chỉ `line` (số hiển thị) được đánh lại cho đúng thứ tự.
    """
    segments = project.setdefault("segments", [])
    next_id = max((int(seg["id"]) for seg in segments), default=0) + 1
    fresh: List[Dict] = []
    for item in found:
        fresh.append({
            "id": next_id, "line": next_id,
            "start_ms": int(item["start_ms"]),
            "orig_start_ms": int(item["start_ms"]),
            "orig_end_ms": int(item["end_ms"]),
            "source_text": item["text"], "target_text": "",
            "clip": "", "clip_ms": 0, "fit_ms": 0,
            "gain": 1.0, "muted": False, "speed": 1.0,
            "discovered": True,
        })
        next_id += 1
    segments.extend(fresh)
    _renumber(project)
    return fresh


def _merge_patch(patches: Dict[int, Dict], seg: Dict, patch: Dict) -> None:
    """Sửa câu đang giữ trong RAM, đồng thời nhớ lại để lát nữa ghi xuống đĩa."""
    seg.update(patch)
    patches.setdefault(int(seg["id"]), {}).update(patch)


def _retranslate(project: Dict, targets: List[Dict], patches: Dict[int, Dict],
                 log: Callable[[str], None], stage: Callable[[str, int], None],
                 base: int, span: int) -> int:
    """Dịch lại lời cho các câu đã chọn, ngữ cảnh vẫn là transcript của cả video."""
    source_lang = (project.get("source_lang") or "auto").split("-")[0]
    target_lang = project.get("target_lang") or "vi"
    if target_lang == source_lang:
        log("Ngôn ngữ đích trùng ngôn ngữ nguồn, bỏ qua bước dịch")
        return 0
    usable = [s for s in targets if (s.get("source_text") or "").strip()]
    if not usable:
        log("Không câu nào còn transcript gốc để dịch lại, giữ nguyên lời hiện có")
        return 0

    cfg = load_config().get("openai", {})
    translator = OpenAITranslator(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
        temperature=cfg.get("temperature", 0.3),
        batch_size=cfg.get("batch_size", 20),
        context_chars=int(cfg.get("context_chars", 12000)),
        keep_terms=cfg.get("keep_terms") or [],
        # Chỉ thị riêng của job được chụp lúc chạy pipeline; job cũ chưa có thì lấy chỉ thị chung
        instruction=((project.get("translate") or {}).get("instruction") or cfg.get("instruction") or ""),
    )
    if not translator.api_key:
        raise EditorError("Chưa cấu hình OpenAI API key nên không dịch lại được")

    context = [_as_item(s) for s in project.get("segments", []) if (s.get("source_text") or "").strip()]
    budgets = {int(s["id"]): _budget_ms(project, s) for s in usable}
    log(f"Dịch lại {len(usable)} câu sang {english_name(target_lang)} | model {translator.model} "
        f"| ngữ cảnh: cả transcript {len(context)} dòng")

    def progress(index: int, total: int, message: str) -> None:
        stage("Dịch lại đoạn", base + int(span * index / max(1, total)))
        log(message)

    try:
        translated = translator.translate(
            [_as_item(s) for s in usable],
            target_name=english_name(target_lang),
            source_name=english_name(source_lang) if source_lang != "auto" else "",
            progress=progress, budgets=budgets, context_items=context)
    except TranslateError as exc:
        raise EditorError(f"Lỗi dịch: {exc}") from exc

    by_id = {int(item["line"]): (item.get("text") or "").strip() for item in translated}
    changed = kept = 0
    for seg in usable:
        text = by_id.get(int(seg["id"]), "")
        # Lô nào hỏng thì translator trả lại nguyên văn câu nguồn - coi như không dịch được
        if not text or text == (seg.get("source_text") or "").strip():
            kept += 1
            continue
        if text != (seg.get("target_text") or ""):
            changed += 1
        _merge_patch(patches, seg, {"target_text": text})
    if kept:
        log(f"{kept} câu không lấy được bản dịch mới, giữ nguyên lời cũ")
    log(f"Đã dịch lại xong: {changed}/{len(usable)} câu đổi lời")
    return changed


def _redub(job_id: str, project: Dict, targets: List[Dict], patches: Dict[int, Dict],
           fit: bool, log: Callable[[str], None], stage: Callable[[str, int], None],
           base: int, span: int) -> Dict[int, List[float]]:
    """Đọc lại toàn bộ câu trong đoạn, chạy song song như bước lồng tiếng của pipeline."""
    client = _tts_client(project)
    speed = max(TTS_SPEED_MIN, min(float(project["tts"].get("speed", 1.0)), TTS_SPEED_MAX))
    steps = max(1, min(int(project["tts"].get("dit_steps", 16)), 64))
    workers = max(1, int(load_config().get("pipeline", {}).get("tts_concurrency", 3)))
    peaks: Dict[int, List[float]] = {}
    failures: List[str] = []
    guard = threading.Lock()
    done = {"n": 0}

    def work(seg: Dict) -> None:
        try:
            text = (seg.get("target_text") or "").strip()
            if not text:
                raise EditorError("chưa có lời để đọc")
            # Câu mới dò ra không có khung đáng tin: VAD cắt theo năng lượng nên khung
            # thường dài hơn lời thật rất nhiều (đuôi nhạc/tiếng động). Ép đọc cho vừa
            # cái khung đó sẽ ra giọng kéo lê - cứ để nó đọc tự nhiên.
            fit_this = fit and not seg.get("discovered")
            _, duration = _fit_duration(seg) if fit_this else (0, None)
            result = _synthesize(job_id, project, seg, client, text, speed, steps, duration)
            with guard:
                _merge_patch(patches, seg, result["patch"])
                peaks[int(seg["id"])] = result["peaks"]
        except EditorError as exc:
            with guard:
                failures.append(f"câu {seg.get('line', seg['id'])} ({exc})")
        finally:
            with guard:
                done["n"] += 1
                stage("Đọc lại đoạn", base + int(span * done["n"] / max(1, len(targets))))
                if done["n"] % 5 == 0 or done["n"] == len(targets):
                    log(f"Đã đọc lại {done['n']}/{len(targets)} câu")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, targets))
    if failures:
        log(f"{len(failures)} câu đọc lại lỗi: " + ", ".join(failures[:8])
            + ("…" if len(failures) > 8 else ""))
    if len(failures) == len(targets):
        raise EditorError(f"Không đọc lại được câu nào trong đoạn: {failures[0]}")
    return peaks


def regenerate_range(job_id: str, ranges: List[Tuple[int, int]], translate: bool, fit: bool,
                     asr: bool, log: Callable[[str], None], stage: Callable[[str, int], None],
                     span: int = 100) -> Dict:
    """Dịch lại + đọc lại mọi câu nằm trong các đoạn đã khoanh.

    Nhiều box được gộp làm MỘT lượt: dịch một lần cho tất cả (rẻ hơn và giữ được
    mạch giữa các đoạn), đọc lại một lần, ghép video một lần.

    Giữ nguyên transcript gốc và mốc từng câu - chỉ lời dịch và file audio là mới.
    `span` là phần trăm tiến độ dành cho việc này (chừa lại cho bước ghép video).
    """
    project = load(job_id)
    where = ", ".join(f"{a / 1000:.1f}s→{b / 1000:.1f}s" for a, b in ranges[:6])
    log(f"Gen lại {len(ranges)} đoạn ({where}{'…' if len(ranges) > 6 else ''})")

    # Chia thanh tiến độ: dò lại 5-25%, dịch 25-45%, TTS 45-100% - tất cả co theo `span`
    discovered: List[Dict] = []
    if asr:
        stage("Nhận dạng lại đoạn trống", int(span * 0.05))
        found = _rescan(job_id, project, ranges, log, stage,
                        base=int(span * 0.05), span=int(span * 0.2))
        discovered = _insert_discovered(project, found)

    targets = segments_in_ranges(project, ranges)
    if not targets:
        raise EditorError("Các đoạn đã khoanh không có câu nào"
                          + (" và cũng không dò ra thoại nào" if asr else ""))
    log(f"Tổng cộng {len(targets)} câu sẽ làm lại"
        + (f", trong đó {len(discovered)} câu mới do ASR dò ra" if discovered else ""))

    patches: Dict[int, Dict] = {}
    if translate:
        stage("Dịch lại đoạn", int(span * 0.25))
        _retranslate(project, targets, patches, log, stage,
                     base=int(span * 0.25), span=int(span * 0.2))
    else:
        log("Bỏ qua bước dịch theo yêu cầu, chỉ đọc lại lời hiện có")
    # Câu mới mà chưa có bản dịch (không dịch, hoặc đích trùng nguồn) thì đọc thẳng lời gốc,
    # nếu không nó sẽ bị bỏ lại thành khoảng lặng y như trước khi dò.
    fallback = 0
    for seg in targets:
        if not (seg.get("target_text") or "").strip() and (seg.get("source_text") or "").strip():
            _merge_patch(patches, seg, {"target_text": seg["source_text"]})
            fallback += 1
    if fallback:
        log(f"{fallback} câu chưa có bản dịch, dùng thẳng lời gốc để đọc")
    peaks = _redub(job_id, project, targets, patches, fit, log, stage,
                   base=int(span * 0.45), span=int(span * 0.5))

    # Ghi xuống đĩa: nạp lại bản mới nhất rồi chỉ đè đúng những trường vừa sinh ra,
    # để thao tác kéo/thả người dùng làm trong lúc chờ không bị mất.
    with _lock_for(job_id):
        latest = load(job_id)
        known = {int(seg["id"]) for seg in latest.get("segments", [])}
        # Câu do ASR dò ra chưa có trên đĩa: chèn vào rồi xếp lại theo thời gian
        added = [dict(seg) for seg in discovered if int(seg["id"]) not in known]
        if added:
            latest.setdefault("segments", []).extend(added)
        by_id = {int(seg["id"]): seg for seg in latest.get("segments", [])}
        for seg_id, patch in patches.items():
            if seg_id in by_id:
                by_id[seg_id].update(patch)
        if added:
            _renumber(latest)
        save(job_id, latest)
        if peaks:
            _store_clip_peaks(job_id, peaks)
    log(f"Xong đoạn: {len(peaks)}/{len(targets)} câu có clip mới")
    # Chỉ trả con số: trạng thái này bị poll mỗi giây, nhét cả câu lẫn peaks vào
    # thì mỗi lần hỏi lại tải về vài chục KB thừa. UI tự nạp lại project khi xong.
    return {"count": len(targets), "redubbed": len(peaks), "ranges": len(ranges),
            "discovered": len(discovered),
            "from_line": targets[0].get("line"), "to_line": targets[-1].get("line")}


# ---------------------------------------------------------------- render lại
def effective_ms(seg: Dict) -> int:
    """Độ dài câu sau khi tính cả yêu cầu ép khung của người dùng."""
    fit = int(seg.get("fit_ms") or 0)
    return fit if fit > 0 else int(seg.get("clip_ms") or 0)


def timeline_ms(project: Dict) -> int:
    end = int(project.get("duration_ms", 0))
    for seg in project.get("segments", []):
        if not seg.get("muted") and seg.get("clip"):
            end = max(end, int(seg.get("start_ms", 0)) + effective_ms(seg))
    return end


def build_dub_track(job_id: str, project: Dict, out_wav: Path,
                    log: Optional[Callable[[str], None]] = None) -> int:
    """Dựng track lồng tiếng bằng cách overlay từng câu vào đúng mốc tuyệt đối của nó."""
    log = log or (lambda _m: None)
    total_ms = timeline_ms(project)
    buffer = np.zeros(max(1, int(total_ms * SR / 1000)), dtype=np.float32)
    tmp_dir = edit_dir(job_id) / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    placed = stretched = 0

    for seg in project.get("segments", []):
        if seg.get("muted") or not seg.get("clip"):
            continue
        try:
            path = resolve(job_id, seg["clip"])
        except EditorError:
            log(f"Câu {seg['id']}: mất file clip, bỏ qua")
            continue
        samples = ffmpeg.decode_pcm(path)
        current_ms = _duration_ms(samples)
        want_ms = int(seg.get("fit_ms") or 0)
        if want_ms > 0 and current_ms > 0 and abs(want_ms - current_ms) > FIT_TOLERANCE_MS:
            factor = current_ms / want_ms
            if 0.25 <= factor <= 4.0:
                fitted = tmp_dir / f"fit_{int(seg['id']):04d}.wav"
                try:
                    ffmpeg.speed_up_audio(path, fitted, want_ms, current_ms)
                    samples = ffmpeg.decode_pcm(fitted)
                    stretched += 1
                except ffmpeg.FFmpegError as exc:
                    log(f"Câu {seg['id']}: ép khung thất bại, giữ nguyên ({exc})")
            else:
                log(f"Câu {seg['id']}: chênh lệch quá lớn (×{factor:.2f}), không ép khung")
        gain = float(seg.get("gain", 1.0) or 0.0)
        start = max(0, int(int(seg.get("start_ms", 0)) * SR / 1000))
        end = start + samples.size
        if end > buffer.size:
            buffer = np.concatenate([buffer, np.zeros(end - buffer.size, dtype=np.float32)])
        buffer[start:end] += samples.astype(np.float32) * gain
        placed += 1

    shutil.rmtree(tmp_dir, ignore_errors=True)
    merged = np.clip(buffer, -32768, 32767).astype(np.int16)
    ffmpeg.write_wav(merged, out_wav)
    log(f"Đã dựng track lồng tiếng: {placed} câu, {stretched} câu ép khung bằng atempo, "
        f"{_duration_ms(merged) / 1000:.1f}s")
    return _duration_ms(merged)


def _background_of(job_id: str, project: Dict) -> Tuple[Optional[Path], bool]:
    """(file nền, nền đã chứa sẵn giọng gốc chưa)."""
    media, mix = project.get("media", {}), project.get("mix", {})
    stem = media.get("background")
    if mix.get("background_source") != "original" and stem:
        try:
            return resolve(job_id, stem), False
        except EditorError:
            pass
    if media.get("source"):
        try:
            return resolve(job_id, media["source"]), True
        except EditorError:
            pass
    return None, True


def _subtitle_of(job_id: str, project: Dict) -> Path:
    items = []
    for seg in project.get("segments", []):
        text = (seg.get("target_text") or "").strip()
        if not text or seg.get("muted"):
            continue
        start = int(seg.get("start_ms", 0))
        items.append(srt.make_item(int(seg["line"]), start, start + max(400, effective_ms(seg)), text))
    path = edit_dir(job_id) / "target.srt"
    path.write_text(srt.to_srt(items) if items else "", encoding="utf-8")
    return path


def render(job_id: str, log: Optional[Callable[[str], None]] = None,
           stage: Optional[Callable[[str, int], None]] = None) -> Dict:
    """Ghép lại video từ project hiện tại. Không chạy lại ASR/dịch/TTS."""
    log = log or (lambda _m: None)
    stage = stage or (lambda _s, _p: None)
    project = load(job_id)
    root = job_dir(job_id)
    edit = edit_dir(job_id)
    mix = project.get("mix", {})
    stem = Path(project.get("filename") or "video").stem
    lang = project.get("target_lang") or "out"

    stage("Dựng track lồng tiếng", 15)
    dub_wav = edit / "dubbed.wav"
    audio_ms = build_dub_track(job_id, project, dub_wav, log)

    final_audio = dub_wav
    if mix.get("mix_original_audio", True):
        stage("Trộn nhạc nền và giọng gốc", 45)
        background, voice_included = _background_of(job_id, project)
        tracks: List[Tuple[Path, float]] = []
        labels: List[str] = []
        if background:
            tracks.append((background, float(mix.get("background_volume", 0.9))))
            labels.append(f"nền ×{mix.get('background_volume', 0.9):.2f}")
        voice_volume = float(mix.get("original_voice_volume", 0.0))
        if voice_volume > 0 and not voice_included and project["media"].get("vocals"):
            try:
                tracks.append((resolve(job_id, project["media"]["vocals"]), voice_volume))
                labels.append(f"giọng gốc ×{voice_volume:.2f}")
            except EditorError:
                pass
        tracks.append((dub_wav, float(mix.get("dubbed_volume", 1.0))))
        labels.append(f"lồng tiếng ×{mix.get('dubbed_volume', 1.0):.2f}")
        mixed = edit / "mixed.wav"
        try:
            ffmpeg.mix_tracks(tracks, mixed, limiter=bool(mix.get("limiter")))
            final_audio = mixed
            log("Đã trộn " + " + ".join(labels))
        except ffmpeg.FFmpegError as exc:
            log(f"Trộn nền thất bại, dùng riêng track lồng tiếng: {exc}")

    stage("Ghép video", 70)
    result: Dict[str, str] = {}
    output = root / "output"
    output.mkdir(parents=True, exist_ok=True)
    video_src: Optional[Path] = None
    if project.get("has_video"):
        try:
            video_src = resolve(job_id, project["media"].get("novoice", ""))
        except EditorError:
            source = project["media"].get("source")
            if source:
                log("Không còn novoice.mp4, tách lại phần hình từ file gốc")
                video_src = edit / "novoice.mp4"
                ffmpeg.extract_video_only(resolve(job_id, source), video_src)

    if not video_src:
        out_path = output / f"{stem}-{lang}-edit.m4a"
        ffmpeg.audio_only_output(final_audio, out_path)
        result = {"name": out_path.name, "kind": "audio"}
    else:
        video_ms = ffmpeg.media_duration_ms(video_src)
        if audio_ms > video_ms + 200:
            log(f"Lồng tiếng dài hơn hình {(audio_ms - video_ms) / 1000:.1f}s, kéo dài bằng khung cuối")
            extended = edit / "novoice_extended.mp4"
            try:
                ffmpeg.extend_video(video_src, extended, audio_ms - video_ms + 100)
                video_src = extended
            except ffmpeg.FFmpegError as exc:
                log(f"Kéo dài video thất bại, hình sẽ bị cắt theo độ dài gốc: {exc}")
        burn = bool(mix.get("burn_subtitle"))
        soft = bool(mix.get("soft_subtitle")) and not burn
        subtitle = _subtitle_of(job_id, project) if (burn or soft) else None
        out_path = output / f"{stem}-{lang}-edit.mp4"
        try:
            ffmpeg.mux(video_src, final_audio, out_path, subtitle=subtitle,
                       burn_subtitle=burn, copy_video=not burn)
        except ffmpeg.FFmpegError as exc:
            log(f"Ghép có phụ đề lỗi ({exc}), ghép lại không phụ đề")
            ffmpeg.mux(video_src, final_audio, out_path, copy_video=True)
        result = {"name": out_path.name, "kind": "video"}

    # File trung gian to bằng cả bản không nén: ghép xong là bỏ, lần render sau
    # dựng lại từ project.json trong vài giây.
    for junk in ("dubbed.wav", "mixed.wav", "novoice_extended.mp4"):
        (edit / junk).unlink(missing_ok=True)

    stage("Hoàn tất", 100)
    with _lock_for(job_id):
        project = load(job_id)
        project["output"] = {**result, "at": time.time(),
                             "size": out_path.stat().st_size if out_path.exists() else 0}
        save(job_id, project)
    log(f"Xong: output/{out_path.name}")
    return project["output"]


# --------------------------------------------------- việc chạy nền + trạng thái
# Mỗi job chỉ chạy một việc nặng tại một thời điểm (ghép video, gen lại một đoạn):
# cả hai đều ghi vào project.json và edit/, chạy song song là đá nhau.
_tasks: Dict[str, Dict] = {}


def render_status(job_id: str) -> Dict:
    """Trạng thái việc đang chạy của job. Tên giữ nguyên vì UI vẫn poll đường dẫn cũ."""
    return _tasks.get(job_id) or {"running": False, "kind": "", "progress": 0, "stage": "",
                                  "logs": [], "error": "", "output": {}}


def _start_task(job_id: str, kind: str,
                work: Callable[[Callable[[str], None], Callable[[str, int], None]], Optional[Dict]]) -> Dict:
    with _locks_guard:
        current = _tasks.get(job_id)
        if current and current.get("running"):
            running = "ghép video" if current.get("kind") == "render" else "gen lại một đoạn"
            raise EditorError(f"Job này đang {running}, chờ lượt đó xong đã")
        state = {"running": True, "kind": kind, "progress": 0, "stage": "Bắt đầu", "logs": [],
                 "error": "", "output": {}, "started_at": time.time()}
        _tasks[job_id] = state

    def log(message: str) -> None:
        state["logs"].append(message)
        del state["logs"][:-200]

    def stage(name: str, progress: int) -> None:
        state["stage"] = name
        state["progress"] = max(state["progress"], int(progress))

    def run() -> None:
        try:
            state["output"] = work(log, stage) or {}
        except (EditorError, TranslateError, TtsError, ffmpeg.FFmpegError, OSError) as exc:
            state["error"] = str(exc)
            log(f"Lỗi: {exc}")
        except Exception as exc:  # noqa: BLE001 - lỗi lạ cũng phải hiện lên UI
            state["error"] = f"{type(exc).__name__}: {exc}"
            log(state["error"])
        finally:
            state["running"] = False
            state["ended_at"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return state


def start_render(job_id: str) -> Dict:
    return _start_task(job_id, "render", lambda log, stage: render(job_id, log=log, stage=stage))


def start_range_regen(job_id: str, ranges: List[Tuple[int, int]], translate: bool = True,
                      fit: bool = True, render_after: bool = True, asr: bool = False) -> Dict:
    """Gen lại các đoạn đã khoanh trong luồng nền, xong thì ghép lại video luôn.

    Kiểm tra đoạn ngay tại đây (chưa vào luồng nền) để bấm nhầm là báo lỗi liền,
    không phải chờ poll mới biết.
    """
    clean: List[Tuple[int, int]] = []
    for start_ms, end_ms in ranges or []:
        start_ms, end_ms = max(0, int(start_ms)), max(0, int(end_ms))
        if end_ms - start_ms >= 200:
            clean.append((start_ms, end_ms))
    if not clean:
        raise EditorError("Chưa có box nào đủ rộng - chèn box trên dải Audio gốc rồi kéo rộng ra")
    project = load(job_id)
    if not segments_in_ranges(project, clean):
        # Bật ASR thì box rỗng lại chính là mục tiêu - miễn là nó có chỗ trống đủ dài
        if not asr:
            raise EditorError("Các đoạn đã khoanh không có câu nào")
        if not gaps_in_ranges(project, clean):
            raise EditorError("Các đoạn đã khoanh không có câu nào và cũng không đủ dài để dò lại")

    def work(log: Callable[[str], None], stage: Callable[[str, int], None]) -> Dict:
        # Chừa 30% cuối cho ffmpeg khi còn phải ghép lại video
        span = 70 if render_after else 100
        result = regenerate_range(job_id, clean, translate, fit, asr, log, stage, span=span)
        if not render_after:
            stage("Hoàn tất", 100)
            return {"range": result}
        log("Bắt đầu ghép lại video với các đoạn vừa gen")
        output = render(job_id, log=log,
                        stage=lambda name, percent: stage(name, span + int(percent * (100 - span) / 100)))
        return {**output, "range": result}

    return _start_task(job_id, "range", work)
