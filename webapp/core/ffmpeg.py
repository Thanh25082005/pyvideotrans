"""Bọc các thao tác ffmpeg/ffprobe dùng trong pipeline.

Cách làm bám theo videotrans/task/_stage_prepare.py, _rate.py và _stage_assemble.py:
tách audio 16k mono để nhận dạng, tách video không tiếng để ghép lại ở bước cuối,
tăng tốc audio bằng atempo, và ghép video+audio ở bước assemble.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

SAMPLE_RATE = 48000  # sample rate của track lồng tiếng cuối cùng
CHANNELS = 1


class FFmpegError(RuntimeError):
    pass


def _run(cmd: Sequence[str], capture_stdout: bool = False, timeout: Optional[int] = 3600):
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise FFmpegError("\n".join(tail[-6:]) or f"ffmpeg exit {proc.returncode}")
    return proc.stdout


def run_ffmpeg(args: Sequence[str], timeout: Optional[int] = 3600) -> None:
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", *args], timeout=timeout)


def probe(path: str | Path) -> dict:
    out = _run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_stdout=True,
        timeout=120,
    )
    data = json.loads(out.decode("utf-8", "ignore") or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    fps = 25.0
    if video and video.get("r_frame_rate", "0/0") != "0/0":
        try:
            num, den = video["r_frame_rate"].split("/")
            if float(den):
                fps = float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass
    return {
        "duration_ms": int(duration * 1000),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "channels": int(audio.get("channels", 0) or 0) if audio else 0,
        "sample_rate": int(audio.get("sample_rate", 0) or 0) if audio else 0,
        "fps": fps,
        "video_codec": (video or {}).get("codec_name", ""),
        "pix_fmt": (video or {}).get("pix_fmt", ""),
    }


def media_duration_ms(path: str | Path) -> int:
    return probe(path)["duration_ms"]


def extract_audio_16k(src: str | Path, out_wav: str | Path) -> str:
    """Audio 16k mono pcm_s16le - đầu vào cho VAD và cho API nhận dạng."""
    run_ffmpeg(["-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)])
    return str(out_wav)


def extract_video_only(src: str | Path, out_mp4: str | Path, info: Optional[dict] = None) -> str:
    """Video không tiếng (novoice.mp4). Copy stream nếu đã là h264/yuv420p."""
    info = info or probe(src)
    can_copy = info.get("video_codec") == "h264" and info.get("pix_fmt") == "yuv420p"
    cmd = ["-y", "-fflags", "+genpts", "-i", str(src), "-an"]
    cmd += ["-c:v", "copy"] if can_copy else ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast"]
    cmd.append(str(out_mp4))
    try:
        run_ffmpeg(cmd)
    except FFmpegError:
        if can_copy:  # copy thất bại -> encode lại
            run_ffmpeg(["-y", "-i", str(src), "-an", "-c:v", "libx264", "-crf", "18",
                        "-preset", "veryfast", str(out_mp4)])
        else:
            raise
    return str(out_mp4)


def cut_audio(src: str | Path, start_ms: int, end_ms: int, out_path: str | Path,
              sample_rate: int = 16000, channels: int = 1) -> str:
    duration = max(0.05, (end_ms - start_ms) / 1000.0)
    run_ffmpeg([
        "-y", "-ss", f"{start_ms / 1000.0:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
        "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(out_path),
    ])
    return str(out_path)


def audio_layout(path: str | Path) -> Tuple[int, int]:
    """(số kênh, sample rate) của luồng audio đầu tiên. (0, 0) nếu không có."""
    try:
        out = _run(
            [FFPROBE, "-v", "quiet", "-select_streams", "a:0", "-print_format", "json",
             "-show_streams", str(path)],
            capture_stdout=True, timeout=60,
        )
        streams = json.loads(out.decode("utf-8", "ignore") or "{}").get("streams") or []
    except (FFmpegError, ValueError):
        return 0, 0
    if not streams:
        return 0, 0
    return int(streams[0].get("channels", 0) or 0), int(streams[0].get("sample_rate", 0) or 0)


def _layout_name(channels: int) -> str:
    return "mono" if channels <= 1 else "stereo"


def decode_pcm(path: str | Path, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> np.ndarray:
    """Giải mã file audio bất kỳ thành mảng int16 mono."""
    raw = _run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", str(channels), "-ar", str(sample_rate), "-"],
        capture_stdout=True,
    )
    return np.frombuffer(raw, dtype="<i2").copy()


def write_wav(samples: np.ndarray, path: str | Path, sample_rate: int = SAMPLE_RATE,
              channels: int = CHANNELS) -> str:
    """Ghi mảng int16 ra file wav thông qua ffmpeg (không cần thư viện ngoài)."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(sample_rate),
         "-ac", str(channels), "-i", "pipe:0", "-c:a", "pcm_s16le", str(path)],
        input=samples.astype("<i2").tobytes(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        raise FFmpegError("\n".join(tail[-6:]))
    return str(path)


def apply_volume(src: str | Path, out_path: str | Path, volume: float = 1.0) -> str:
    """Đổi âm lượng một track, giữ nguyên số kênh và sample rate của nó."""
    volume = max(0.0, min(float(volume), 4.0))
    channels, rate = audio_layout(src)
    run_ffmpeg([
        "-y", "-i", str(src), "-filter:a", f"volume={volume:.3f}",
        "-ac", str(channels or CHANNELS), "-ar", str(rate or SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(out_path),
    ])
    return str(out_path)


def count_clipped(path: str | Path) -> Tuple[int, int]:
    """(số mẫu chạm trần, tổng số mẫu). Dùng để cảnh báo thay vì âm thầm nén."""
    # Đọc đúng sample rate và số kênh gốc: resample/downmix sẽ làm đỉnh đổi giá trị
    channels, rate = audio_layout(path)
    samples = decode_pcm(path, sample_rate=rate or SAMPLE_RATE, channels=channels or CHANNELS)
    if samples.size == 0:
        return 0, 0
    return int((np.abs(samples.astype(np.int32)) >= 32767).sum()), int(samples.size)


def atempo_chain(factor: float) -> str:
    """atempo chỉ nhận 0.5-2.0 nên phải nối nhiều filter khi vượt ngưỡng."""
    factor = max(0.25, min(float(factor), 8.0))
    parts: List[str] = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.6f}")
    return ",".join(parts)


def speed_up_audio(src: str | Path, out_path: str | Path, target_ms: int,
                   current_ms: Optional[int] = None) -> str:
    """Ép một đoạn lồng tiếng về đúng target_ms (chỉ dùng khi cần rút ngắn)."""
    current_ms = current_ms or media_duration_ms(src)
    if target_ms <= 0 or current_ms <= 0:
        return str(src)
    factor = current_ms / target_ms
    run_ffmpeg([
        "-y", "-i", str(src), "-filter:a", atempo_chain(factor),
        "-t", f"{target_ms / 1000.0:.3f}", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-c:a", "pcm_s16le", str(out_path),
    ])
    return str(out_path)


def extend_video(src: str | Path, out_path: str | Path, extra_ms: int) -> str:
    """Kéo dài video bằng cách giữ khung hình cuối (khi lồng tiếng dài hơn video)."""
    seconds = max(0.04, extra_ms / 1000.0)
    run_ffmpeg([
        "-y", "-i", str(src), "-vf", f"tpad=stop_mode=clone:stop_duration={seconds:.3f}",
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-an", str(out_path),
    ])
    return str(out_path)


def mux(video: str | Path, audio: str | Path, out_path: str | Path,
        subtitle: Optional[str | Path] = None, burn_subtitle: bool = False,
        copy_video: bool = True) -> str:
    """Ghép video không tiếng + track lồng tiếng (+ phụ đề) thành file kết quả."""
    cmd = ["-y", "-i", str(video), "-i", str(audio)]
    if burn_subtitle and subtitle:
        escaped = str(subtitle).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        cmd += [
            "-filter_complex", f"[0:v]subtitles=filename='{escaped}'[v]",
            "-map", "[v]", "-map", "1:a",
            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        ]
    else:
        if subtitle:
            cmd += ["-i", str(subtitle)]
        cmd += ["-map", "0:v", "-map", "1:a"]
        if subtitle:
            cmd += ["-map", "2:s"]
        cmd += ["-c:v", "copy" if copy_video else "libx264"]
        if not copy_video:
            cmd += ["-crf", "20", "-preset", "veryfast"]
        if subtitle:
            cmd += ["-c:s", "mov_text"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    # -shortest sẽ cắt video theo stream phụ đề (kết thúc ở câu cuối) nên chỉ dùng khi không nhúng sub
    if not (subtitle and not burn_subtitle):
        cmd.append("-shortest")
    cmd.append(str(out_path))
    run_ffmpeg(cmd)
    return str(out_path)


def mix_tracks(tracks: Sequence[Tuple[str | Path, float]], out_path: str | Path,
               channels: Optional[int] = None, sample_rate: Optional[int] = None,
               limiter: bool = False) -> str:
    """Trộn nhiều track, mỗi track một hệ số âm lượng riêng.

    Track có âm lượng 0 bị bỏ hẳn khỏi filter thay vì nhân 0 - đỡ một lần giải mã
    và tránh việc `duration=longest` bị kéo dài bởi một track câm.

    Layout lấy theo track ĐẦU TIÊN (nền), nên phim stereo 44,1kHz ra đúng stereo
    44,1kHz. `normalize=0` để con số âm lượng đúng nghĩa: mặc định của amix là
    chia biên độ cho số input, tức là đặt 1.0 vẫn bị hạ tiếng.
    """
    active = [(Path(src), max(0.0, min(float(vol), 4.0)))
              for src, vol in tracks if src and float(vol or 0) > 0.0005]
    if not active:
        raise FFmpegError("mix_tracks: không còn track nào có âm lượng > 0")

    ref_channels, ref_rate = audio_layout(active[0][0])
    channels = int(channels or ref_channels or CHANNELS)
    sample_rate = int(sample_rate or ref_rate or SAMPLE_RATE)
    layout = _layout_name(channels)
    channels = 1 if layout == "mono" else 2
    fmt = f"aformat=sample_fmts=fltp:channel_layouts={layout}:sample_rates={sample_rate}"

    cmd: List[str] = ["-y"]
    for src, _ in active:
        cmd += ["-i", str(src)]
    parts = [f"[{i}:a]volume={vol:.3f},{fmt}[a{i}]" for i, (_, vol) in enumerate(active)]
    labels = "".join(f"[a{i}]" for i in range(len(active)))
    if len(active) == 1:
        chain = parts[0].replace(f"[a0]", "[mixed]")
    else:
        chain = ";".join(parts) + (
            f";{labels}amix=inputs={len(active)}:duration=longest:"
            "dropout_transition=0:normalize=0[mixed]")
    chain += ";[mixed]alimiter=limit=0.97:level=disabled[out]" if limiter else ";[mixed]anull[out]"
    run_ffmpeg(cmd + [
        "-filter_complex", chain, "-map", "[out]",
        "-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(out_path),
    ])
    return str(out_path)


def mix_audio(dubbed: str | Path, original: str | Path, out_path: str | Path,
              original_volume: float = 0.35, dubbed_volume: float = 1.0,
              channels: Optional[int] = None, sample_rate: Optional[int] = None,
              limiter: bool = False) -> str:
    """Trộn track lồng tiếng lên trên một track nền (bọc mix_tracks cho gọn)."""
    return mix_tracks([(original, original_volume), (dubbed, dubbed_volume)],
                      out_path, channels=channels, sample_rate=sample_rate, limiter=limiter)


def audio_only_output(audio: str | Path, out_path: str | Path) -> str:
    run_ffmpeg(["-y", "-i", str(audio), "-c:a", "aac", "-b:a", "192k", str(out_path)])
    return str(out_path)
