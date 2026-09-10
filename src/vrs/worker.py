"""FastAPI 后台跑流水线：HTTP 立刻返回，线程里持 JobLock。"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from vrs.cancel import JobCancelled, clear_cancel
from vrs.jobstore import get_job, save_status
from vrs.lock import BusyError
from vrs.settings import Settings

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def _append_worker_log(settings: Settings, job_id: str, text: str) -> None:
    try:
        directory, _job = get_job(settings, job_id)
    except FileNotFoundError:
        return
    path = directory / "logs" / "worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def _mark_failed(settings: Settings, job_id: str, message: str) -> None:
    try:
        directory, job = get_job(settings, job_id)
    except FileNotFoundError:
        return
    if job.get("state") == "running":
        job["state"] = "failed"
    job["note"] = message
    save_status(job, directory)


def spawn(settings: Settings, job_id: str, fn: Callable[[], Any]) -> None:
    def run() -> None:
        try:
            fn()
        except JobCancelled:
            try:
                directory, job = get_job(settings, job_id)
                job["state"] = "cancelled"
                job["note"] = "用户放弃"
                stage = str(job.get("stage") or "download")
                rec = (job.get("stages") or {}).setdefault(stage, {})
                if rec.get("status") == "running":
                    rec["status"] = "pending"
                    rec["error"] = "用户放弃"
                save_status(job, directory)
            except FileNotFoundError:
                pass
        except BusyError as exc:
            _mark_failed(settings, job_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            _append_worker_log(settings, job_id, traceback.format_exc())
            _mark_failed(settings, job_id, str(exc) or "后台任务异常退出")
        finally:
            with _lock:
                _threads.pop(job_id, None)

    thread = threading.Thread(target=run, name=f"vrs-{job_id}", daemon=True)
    with _lock:
        existing = _threads.get(job_id)
        if existing is not None and existing.is_alive():
            raise BusyError(f"已有任务在跑：{job_id}")
        thread.start()
        _threads[job_id] = thread


def spawn_resume(settings: Settings, job_id: str, **kwargs: Any) -> None:
    from vrs.runner import resume_download

    def fn() -> None:
        directory, _ = get_job(settings, job_id)
        clear_cancel(directory)
        resume_download(settings, job_id, **kwargs)

    spawn(settings, job_id, fn)
