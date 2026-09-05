"""Local HTTP service host Qwen3-ForcedAligner-0.6B và Hybrid Demucs trên GPU.

Hai model đều cần torch nên ở chung một venv (.aligner-venv). Webapp chính giữ
nguyên chỉ-numpy và gọi sang đây qua HTTP.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import threading
from pathlib import Path

import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

MODEL_NAME = os.getenv("QWEN_ALIGNER_MODEL", "Qwen/Qwen3-ForcedAligner-0.6B")

app = FastAPI(title="Qwen3 Forced Aligner + Demucs", docs_url=None, redoc_url=None)
_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()
_device = "cuda:0" if torch.cuda.is_available() else "cpu"

_demucs = None
_demucs_lock = threading.Lock()
# Cắt khúc để không nổ VRAM: 8 giây/khúc, chồng 1 giây rồi cross-fade lại
DEMUCS_SEGMENT_S = float(os.getenv("DEMUCS_SEGMENT_S", "8.0"))
DEMUCS_OVERLAP_S = float(os.getenv("DEMUCS_OVERLAP_S", "1.0"))


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from qwen_asr import Qwen3ForcedAligner

            dtype = torch.bfloat16 if _device.startswith("cuda") else torch.float32
            _model = Qwen3ForcedAligner.from_pretrained(
                MODEL_NAME,
                dtype=dtype,
                device_map=_device,
            )
    return _model


def get_demucs():
    """Hybrid Demucs đi kèm torchaudio - không cần cài thêm package nào."""
    global _demucs
    if _demucs is not None:
        return _demucs
    with _demucs_lock:
        if _demucs is None:
            from torchaudio.pipelines import HDEMUCS_HIGH_MUSDB_PLUS as bundle

            model = bundle.get_model().to(_device).eval()
            _demucs = (model, bundle.sample_rate, list(model.sources))
    return _demucs


@app.get("/health")
def health():
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": _device,
        "loaded": _model is not None,
        "demucs_loaded": _demucs is not None,
    }


@app.post("/load")
def load_model():
    try:
        get_model()
        return health()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Không load được model: {exc}") from exc


@app.post("/align")
async def align(audio: UploadFile = File(...), text: str = Form(...), language: str = Form(...)):
    transcript = text.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript rỗng")

    suffix = Path(audio.filename or "audio.wav").suffix or ".wav"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
            temp_path = temp.name
            while chunk := await audio.read(1024 * 1024):
                temp.write(chunk)
        model = get_model()
        with _inference_lock, torch.inference_mode():
            results = model.align(audio=temp_path, text=transcript, language=language)
        aligned = results[0] if results else []
        words = [
            {
                "word": str(item.text),
                "start": round(float(item.start_time), 3),
                "end": round(float(item.end_time), 3),
            }
            for item in aligned
        ]
        return {"text": transcript, "language": language, "words": words}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Aligner inference lỗi: {exc}") from exc
    finally:
        await audio.close()
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


class SeparateRequest(BaseModel):
    """Đường dẫn tuyệt đối trên cùng máy - service chỉ nghe 127.0.0.1.

    Cố tình không upload/download: một video 5 phút là hàng trăm MB, đẩy qua HTTP
    chỉ tổ chậm khi hai tiến trình dùng chung ổ đĩa.
    """

    input_path: str
    vocals_path: str
    accompaniment_path: str = ""


def _separate_chunked(model, wav, sample_rate: int):
    """Chạy Demucs theo từng khúc có chồng lấn rồi cross-fade, tránh nổ VRAM."""
    segment = int(DEMUCS_SEGMENT_S * sample_rate)
    overlap = int(DEMUCS_OVERLAP_S * sample_rate)
    step = max(1, segment - overlap)
    total = wav.shape[-1]
    n_sources = len(model.sources)
    out = torch.zeros(n_sources, wav.shape[0], total)
    weight = torch.zeros(total)
    ramp = torch.linspace(0.0, 1.0, overlap) if overlap > 0 else torch.zeros(0)

    start = 0
    while start < total:
        end = min(start + segment, total)
        chunk = wav[:, start:end].unsqueeze(0).to(_device)
        with torch.inference_mode():
            estimated = model(chunk)[0].cpu()
        # cửa sổ tam giác ở hai mép để nối khúc không nghe thấy đường ghép
        window = torch.ones(end - start)
        if overlap > 0:
            head = min(overlap, window.numel())
            if start > 0:
                window[:head] = ramp[:head]
            if end < total:
                window[-head:] = ramp[:head].flip(0)
        out[..., start:end] += estimated * window
        weight[start:end] += window
        if end >= total:
            break
        start += step
    return out / weight.clamp(min=1e-6)


@app.post("/separate")
def separate(req: SeparateRequest):
    """Tách giọng khỏi nhạc nền bằng Hybrid Demucs.

    Ghi ra vocals (để VAD/ASR chạy trên giọng sạch) và accompaniment (nhạc nền +
    hiệu ứng, để trộn lại vào bản lồng tiếng mà không dính giọng gốc).
    """
    import torchaudio

    src = Path(req.input_path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"Không thấy file: {src}")
    try:
        model, model_sr, sources = get_demucs()
        wav, sample_rate = torchaudio.load(str(src))
        if wav.shape[0] == 1:
            wav = wav.repeat(2, 1)      # Demucs được huấn luyện trên stereo
        elif wav.shape[0] > 2:
            wav = wav[:2]
        if sample_rate != model_sr:
            wav = torchaudio.functional.resample(wav, sample_rate, model_sr)

        # chuẩn hoá theo thống kê cả bài, đúng như cách model được huấn luyện
        mean, std = wav.mean(), wav.std().clamp(min=1e-8)
        with _inference_lock:
            stems = _separate_chunked(model, (wav - mean) / std, model_sr)
        stems = stems * std + mean

        idx = sources.index("vocals")
        vocals = stems[idx]
        accompaniment = stems.sum(dim=0) - vocals

        Path(req.vocals_path).parent.mkdir(parents=True, exist_ok=True)
        torchaudio.save(req.vocals_path, vocals, model_sr)
        if req.accompaniment_path:
            Path(req.accompaniment_path).parent.mkdir(parents=True, exist_ok=True)
            torchaudio.save(req.accompaniment_path, accompaniment, model_sr)
        return {
            "ok": True,
            "sample_rate": model_sr,
            "duration": round(wav.shape[-1] / model_sr, 2),
            "vocals": req.vocals_path,
            "accompaniment": req.accompaniment_path,
        }
    except HTTPException:
        raise
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(
            status_code=507,
            detail=f"Hết VRAM khi tách nhạc, giảm DEMUCS_SEGMENT_S (đang {DEMUCS_SEGMENT_S}s): {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Tách nhạc lỗi: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
