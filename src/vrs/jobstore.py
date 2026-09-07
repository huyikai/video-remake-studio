from __future__ import annotations

import json
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vrs.h3grid import merge_t2va_snapshot, normalize_generate_path
from vrs.lock import atomic_write_json
from vrs.pathsutil import jobs_dir
from vrs.settings import Settings

STAGES = ("download", "pagemeta", "understand", "script", "precheck", "generate", "finish")


def only_clips(job: dict[str, Any]) -> list[str]:
    """options.only_clips：只写/生成这些段，用来快速验证。"""
    raw = (job.get("options") or {}).get("only_clips")
    if not raw:
        return []
    if isinstance(raw, str):
        items = [part.strip() for part in raw.replace(";", ",").split(",")]
    else:
        items = [str(part).strip() for part in raw]
    return [item for item in items if item]


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def job_dir(settings: Settings, job_id: str) -> Path:
    return jobs_dir(settings) / job_id


def load_status(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_status(job: dict[str, Any], directory: Path) -> None:
    job["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    atomic_write_json(directory / "status.json", job)


def create_job(
    settings: Settings,
    *,
    kind: str,
    url: str | None = None,
    original_path: str | None = None,
    review_mode: str | None = None,
    generate_path: str = "t2va",
    smtp: bool | None = None,
    vl_mode: str | None = None,
    aspect_ratio: str | None = None,
    aspect_confirmed: bool = False,
    generate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_id = new_job_id()
    directory = job_dir(settings, job_id)
    (directory / "source").mkdir(parents=True, exist_ok=True)
    (directory / "logs").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    path = normalize_generate_path(generate_path)
    options: dict[str, Any] = {
        "review_mode": review_mode or settings.default.get("review_mode", "pause_draft"),
        "generate_path": path,
        "smtp": settings.smtp.get("enabled") if smtp is None else smtp,
        "vl_mode": vl_mode or settings.default.get("vl_mode", "both"),
        "aspect_ratio": aspect_ratio or settings.default.get("aspect_ratio", "16:9"),
        "aspect_confirmed": bool(aspect_confirmed),
        "mode": settings.mode(),
        "mock_speed": settings.mock_speed(),
        "mock_faults": deepcopy(settings.default.get("mock_faults") or {}),
    }
    if path == "t2va":
        options["generate"] = merge_t2va_snapshot(settings, generate)
    job = {
        "id": job_id,
        "created_at": now,
        "updated_at": now,
        "state": "pending",
        "stage": "download",
        "stages": {
            name: {"status": "pending", "error": None, "started_at": None, "finished_at": None}
            for name in STAGES
        },
        "source": {
            "kind": kind,
            "url": url,
            "original_path": original_path,
            "video": "source/video.mp4",
        },
        "options": options,
    }
    save_status(job, directory)
    return job


def iter_jobs(settings: Settings) -> list[dict[str, Any]]:
    root = jobs_dir(settings)
    if not root.is_dir():
        return []
    jobs: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        status = child / "status.json"
        if child.is_dir() and status.is_file():
            try:
                jobs.append(load_status(status))
            except (OSError, json.JSONDecodeError):
                continue
    return jobs


def get_job(settings: Settings, job_id: str) -> tuple[Path, dict[str, Any]]:
    directory = job_dir(settings, job_id)
    status = directory / "status.json"
    if not status.is_file():
        raise FileNotFoundError(job_id)
    return directory, load_status(status)


def mark_stage(
    job: dict[str, Any],
    directory: Path,
    stage: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    rec = job["stages"].setdefault(stage, {})
    rec["status"] = status
    rec["error"] = error
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if status == "running" and not rec.get("started_at"):
        rec["started_at"] = now
    if status in {"done", "failed", "skipped"}:
        rec["finished_at"] = now
    job["stage"] = stage
    if status == "failed":
        job["state"] = "failed"
    elif status == "running":
        job["state"] = "running"
    save_status(job, directory)
