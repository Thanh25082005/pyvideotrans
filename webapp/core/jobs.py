"""Quản lý job: chạy pipeline trong thread riêng, gom log và tiến độ cho UI."""
from __future__ import annotations

import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .settings import job_dir, load_config

MAX_LOGS = 4000


class JobCancelled(RuntimeError):
    pass


@dataclass
class Job:
    filename: str
    source_path: str
    params: Dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"          # queued | running | done | error | cancelled
    stage: str = "Đang chờ"
    progress: int = 0
    error: str = ""
    result: Dict = field(default_factory=dict)
    previews: Dict = field(default_factory=dict)
    logs: List[Dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    ended_at: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def dir(self) -> Path:
        return job_dir(self.id)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.ended_at or time.time()) - self.started_at

    def log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.logs.append({"t": time.time(), "level": level, "msg": str(message)})
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    def set_stage(self, stage: str, progress: int) -> None:
        with self._lock:
            self.stage = stage
            self.progress = max(self.progress, min(int(progress), 100))

    def set_preview(self, key: str, text: str) -> None:
        with self._lock:
            self.previews[key] = text

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled("Người dùng đã dừng tiến trình")

    def snapshot(self, log_from: int = 0) -> Dict:
        with self._lock:
            logs = self.logs[max(0, log_from):]
            return {
                "id": self.id,
                "filename": self.filename,
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "error": self.error,
                "result": dict(self.result),
                "elapsed": round(self.elapsed, 1),
                "created_at": self.created_at,
                "params": self.params,
                "log_total": len(self.logs),
                "logs": logs,
            }


class JobManager:
    def __init__(self, max_history: int = 30):
        self.jobs: Dict[str, Job] = {}
        self.order: List[str] = []
        self.max_history = max_history
        self._lock = threading.Lock()

    def create(self, filename: str, source_path: str, params: Dict) -> Job:
        job = Job(filename=filename, source_path=source_path, params=params)
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._evict_locked()
        return job

    def _evict_locked(self) -> None:
        while len(self.order) > self.max_history:
            old_id = self.order.pop(0)
            old = self.jobs.pop(old_id, None)
            if old and old.status in ("done", "error", "cancelled"):
                shutil.rmtree(job_dir(old_id), ignore_errors=True)

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def list(self) -> List[Dict]:
        with self._lock:
            ids = [i for i in reversed(self.order) if i in self.jobs]
        return [{
            "id": job.id,
            "filename": job.filename,
            "status": job.status,
            "progress": job.progress,
            "stage": job.stage,
            "elapsed": round(job.elapsed, 1),
            "created_at": job.created_at,
            "result": dict(job.result),
        } for job in (self.jobs[i] for i in ids)]

    def start(self, job: Job) -> None:
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: Job) -> None:
        from .pipeline import PipelineError, run_pipeline

        job.status = "running"
        job.started_at = time.time()
        job.log(f"Bắt đầu xử lý: {job.filename}")
        try:
            job.result = run_pipeline(job, load_config())
            job.status = "done"
            job.set_stage("Hoàn tất", 100)
        except JobCancelled as exc:
            job.status = "cancelled"
            job.error = str(exc)
            job.log(str(exc), "warn")
        except PipelineError as exc:
            job.status = "error"
            job.error = str(exc)
            job.log(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - hiển thị mọi lỗi còn lại cho người dùng
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(job.error, "error")
            job.log(traceback.format_exc(limit=6), "error")
        finally:
            job.ended_at = time.time()
            if job.status in ("done", "cancelled"):
                shutil.rmtree(job.dir / "cache", ignore_errors=True)
            job.log(f"Kết thúc với trạng thái: {job.status} sau {job.elapsed:.1f}s")


manager = JobManager()
