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

from core import editor, langs
from core.editor import EditorError
from core.forced_aligner import ForcedAlignerError, QwenForcedAlignerClient
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


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """Bắt trình duyệt hỏi lại server mỗi lần lấy html/css/js.

    Không có header này, trình duyệt được phép tự đoán thời hạn cache và dùng lại
    file cũ mà không hỏi. Sửa code xong thì gặp cảnh HTML mới đi cùng JS cũ:
    trang vẫn hiện nút mới nhưng bấm không ăn, mà console cũng chẳng báo gì rõ.
    `no-cache` không tắt cache - chỉ buộc kiểm tra lại, file không đổi vẫn trả 304.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path in ("/", "/editor"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/editor")
def editor_page():
    return FileResponse(STATIC_DIR / "editor.html")


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


@app.get("/api/aligner/status")
def aligner_status():
    cfg = load_config().get("aligner", {})
    if not cfg.get("enabled", True):
        return {"ok": False, "enabled": False, "message": "Qwen Forced Aligner đang tắt"}
    try:
        status = QwenForcedAlignerClient(cfg.get("base_url", "http://127.0.0.1:8200")).health()
        return {**status, "enabled": True}
    except ForcedAlignerError as exc:
        return {"ok": False, "enabled": True, "message": str(exc)}


# --------------------------------------------------------------------- job
@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("vi"),
    voice_id: str = Form(""),
    speed: float = Form(1.0),
    dit_steps: int = Form(16),
    max_audio_speed: float = Form(1.6),
    background_volume: float = Form(-1.0),
    dubbed_volume: float = Form(1.0),
    background_source: str = Form("auto"),
    original_voice_volume: float = Form(0.0),
    instruction: str = Form(""),
    voice_autorate: bool = Form(True),
    mix_original_audio: bool = Form(True),
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
        # Loly nhận dit_steps 1-64; càng cao giọng càng mượt và càng tốn quota
        "dit_steps": max(1, min(int(dit_steps), 64)),
        "max_audio_speed": max_audio_speed,
        # <0 nghĩa là "dùng mặc định của cấu hình" (khác 0 = tắt hẳn tiếng nền)
        "background_volume": None if background_volume < 0 else background_volume,
        "dubbed_volume": dubbed_volume,
        "background_source": (background_source or "auto").strip().lower(),
        # 0 = thay hẳn giọng gốc (lồng tiếng thường); >0 = giọng gốc phát cùng TTS
        "original_voice_volume": original_voice_volume,
        # Rỗng = dùng chỉ thị chung trong cấu hình
        "instruction": (instruction or "").strip(),
        "voice_autorate": voice_autorate,
        "mix_original_audio": mix_original_audio,
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


@app.get("/api/jobs/{job_id}/words")
def job_words(job_id: str):
    """Word-level timestamps cho UI - đọc được ngay trong lúc job còn đang chạy."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return job.words_snapshot()


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


# ------------------------------------------------------- trình chỉnh sửa
@app.exception_handler(EditorError)
def editor_error(_request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/projects")
def project_list():
    """Đọc thẳng từ đĩa nên job cũ vẫn mở lại được sau khi restart server."""
    return {"projects": editor.list_projects()}


@app.get("/api/projects/{job_id}")
def project_get(job_id: str):
    return {"project": editor.load(job_id)}


@app.put("/api/projects/{job_id}")
async def project_update(job_id: str, payload: dict):
    return {"project": editor.update(job_id, payload)}


@app.delete("/api/projects/{job_id}")
def project_delete(job_id: str):
    editor.delete(job_id)
    return {"ok": True}


@app.get("/api/projects/{job_id}/peaks/{name}")
def project_peaks(job_id: str, name: str):
    if name not in ("original", "clips"):
        raise HTTPException(status_code=404, detail="Không có đường bao sóng này")
    return FileResponse(editor.resolve(job_id, f"edit/peaks/{name}.json"), media_type="application/json")


@app.get("/api/projects/{job_id}/media/{path:path}")
def project_media(job_id: str, path: str):
    return FileResponse(editor.resolve(job_id, path))


@app.post("/api/projects/{job_id}/segments/{seg_id}/regen")
async def project_regen(job_id: str, seg_id: int, payload: Optional[dict] = None):
    body = payload or {}
    return editor.regenerate(
        job_id, seg_id,
        text=body.get("text"),
        speed=body.get("speed"),
        fit=bool(body.get("fit")),
        fit_ms=body.get("fit_ms"),
        voice_id=body.get("voice_id"),
        dit_steps=body.get("dit_steps"),
    )


@app.post("/api/projects/{job_id}/segments")
async def project_add_segment(job_id: str, payload: dict):
    """Chèn một câu do người dùng tự gõ lời rồi đọc luôn - chỉ tốn một request TTS."""
    return editor.add_segment(
        job_id,
        start_ms=int(payload.get("start_ms") or 0),
        end_ms=int(payload.get("end_ms") or 0),
        text=str(payload.get("text") or ""),
        source_text=str(payload.get("source_text") or ""),
    )


@app.post("/api/projects/{job_id}/range")
async def project_range(job_id: str, payload: dict):
    """Gen lại các đoạn đã khoanh: dịch lại + đọc lại mọi câu trong đó, rồi ghép lại video.

    Nhận `ranges: [{start_ms, end_ms}, ...]` (nhiều box trên dải Audio gốc) hoặc
    một cặp `start_ms`/`end_ms` cho tiện gọi tay. Chạy nền, tiến độ xem chung ở
    GET /api/projects/{job_id}/render.
    """
    raw = payload.get("ranges")
    if not isinstance(raw, list) or not raw:
        raw = [{"start_ms": payload.get("start_ms"), "end_ms": payload.get("end_ms")}]
    try:
        ranges = [(int(item.get("start_ms") or 0), int(item.get("end_ms") or 0)) for item in raw]
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Danh sách đoạn không hợp lệ")
    return editor.start_range_regen(
        job_id, ranges,
        translate=payload.get("translate", True) is not False,
        fit=payload.get("fit", True) is not False,
        render_after=payload.get("render_after", True) is not False,
        asr=bool(payload.get("asr")),
    )


@app.post("/api/projects/{job_id}/render")
def project_render(job_id: str):
    editor.load(job_id)  # báo lỗi sớm nếu project hỏng
    return editor.start_render(job_id)


@app.get("/api/projects/{job_id}/render")
def project_render_status(job_id: str):
    return editor.render_status(job_id)


@app.get("/api/projects/{job_id}/download")
def project_download(job_id: str, download: int = 1):
    name = (editor.load(job_id).get("output") or {}).get("name")
    if not name:
        raise HTTPException(status_code=404, detail="Chưa render bản chỉnh sửa nào")
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
