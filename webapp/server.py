"""Web UI đơn giản: upload video -> nhận dạng -> dịch -> lồng tiếng -> tải video về.

Chạy:  python webapp/server.py --port 8199
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional

# cho phép chạy `python webapp/server.py` từ bất kỳ thư mục nào
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core import langs
from core.jobs import manager
from core.settings import DATA_DIR, job_dir, load_config, save_config
from core.stt_loli import LoliSTT
from core.translate_openai import OpenAITranslator
from core.tts_loly import LolyTTS, TtsError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg",
               ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

app = FastAPI(title="pyVideoTrans WebApp", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg")


# ----------------------------------------------------------------- cấu hình
@app.get("/api/config")
def get_config():
    config = load_config()
    return {
        "config": config,
        "ready": {
            "openai": bool(config["openai"]["api_key"]),
            "stt": bool(config["stt"]["api_key"]),
            "tts": bool(config["tts"]["api_key"]),
        },
    }


@app.post("/api/config")
async def update_config(payload: dict):
    return {"config": save_config(payload)}


@app.post("/api/config/test/{provider}")
def test_provider(provider: str, payload: Optional[dict] = None):
    config = load_config()
    section = (payload or {}).get(provider) or config.get(provider, {})
    if provider == "stt":
        return LoliSTT(section.get("base_url", ""), section.get("api_key", "")).check_key()
    if provider == "tts":
        return LolyTTS(section.get("base_url", ""), section.get("api_key", ""),
                       section.get("voice_id", "")).check_key()
    if provider == "openai":
        return OpenAITranslator(
            api_key=section.get("api_key", ""),
            base_url=section.get("base_url", ""),
            model=section.get("model", ""),
            temperature=section.get("temperature", 0.3),
        ).check_key()
    raise HTTPException(status_code=404, detail="Provider không hợp lệ")


@app.get("/api/voices")
def list_voices():
    tts_cfg = load_config().get("tts", {})
    client = LolyTTS(tts_cfg.get("base_url", ""), tts_cfg.get("api_key", ""), tts_cfg.get("voice_id", ""))
    if not client.api_key:
        return {"ok": False, "voices": [], "message": "Chưa nhập TTS API key"}
    try:
        voices = client.list_voices()
    except TtsError as exc:
        return {"ok": False, "voices": [], "message": f"Không lấy được danh sách voice: {exc}"}
    if not voices:
        return {"ok": True, "voices": [], "message": "Key không liệt kê được voice nào, hãy nhập voice_id thủ công"}
    return {"ok": True, "voices": voices, "message": f"Tìm thấy {len(voices)} voice"}


@app.get("/api/languages")
def languages():
    return {"source": langs.stt_language_list(), "target": langs.target_language_list()}


# --------------------------------------------------------------------- job
@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("vi"),
    voice_id: str = Form(""),
    speed: float = Form(1.0),
    dit_steps: int = Form(10),
    max_audio_speed: float = Form(1.6),
    voice_autorate: bool = Form(True),
    resynth: bool = Form(True),
    burn_subtitle: bool = Form(False),
    soft_subtitle: bool = Form(False),
    clone_voice: bool = Form(False),
):
    filename = Path(file.filename or "video.mp4").name
    if Path(filename).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Định dạng {Path(filename).suffix} chưa được hỗ trợ")

    config = load_config()
    missing = [name for name, key in (("STT", config["stt"]["api_key"]), ("TTS", config["tts"]["api_key"]))
               if not key]
    if source_lang.split("-")[0] != target_lang and not config["openai"]["api_key"]:
        missing.append("OpenAI")
    if missing:
        raise HTTPException(status_code=400, detail=f"Chưa cấu hình API key cho: {', '.join(missing)}")

    params = {
        "source_lang": source_lang,
        "target_lang": target_lang,
        "voice_id": voice_id.strip(),
        "speed": speed,
        "dit_steps": dit_steps,
        "max_audio_speed": max_audio_speed,
        "voice_autorate": voice_autorate,
        "resynth": resynth,
        "burn_subtitle": burn_subtitle,
        "soft_subtitle": soft_subtitle,
        "clone_voice": clone_voice,
    }
    job = manager.create(filename=filename, source_path="", params=params)
    input_dir = job_dir(job.id) / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    target = input_dir / filename
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=1024 * 1024)
    await file.close()
    job.source_path = str(target)
    job.log(f"Đã nhận file {filename} ({target.stat().st_size / 1024 / 1024:.1f} MB)")
    manager.start(job)
    return {"job_id": job.id}


@app.get("/api/jobs")
def job_list():
    return {"jobs": manager.list()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, log_from: int = 0):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return job.snapshot(log_from=log_from)


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    job.cancel()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/subtitles/{kind}")
def job_subtitles(job_id: str, kind: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return {"text": job.previews.get(kind, "")}


def _output_file(job_id: str, name: str) -> Path:
    base = (job_dir(job_id) / "output").resolve()
    path = (base / name).resolve()
    if not str(path).startswith(str(base)) or not path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy file kết quả")
    return path


@app.get("/api/jobs/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, download: int = 0):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    name = job.result.get(kind)
    if not name:
        raise HTTPException(status_code=404, detail="Kết quả chưa sẵn sàng")
    path = _output_file(job_id, name)
    return FileResponse(path, filename=path.name if download else None)


@app.exception_handler(404)
def not_found(_request, exc):
    return JSONResponse(status_code=404, content={"detail": getattr(exc, "detail", "Not found")})


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="pyVideoTrans WebApp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8199)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n  pyVideoTrans WebApp: http://{args.host}:{args.port}\n")
    uvicorn.run("server:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, app_dir=str(BASE_DIR), log_level="info")


if __name__ == "__main__":
    main()
