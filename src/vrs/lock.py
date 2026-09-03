from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psutil

from vrs.pathsutil import jobs_dir
from vrs.settings import Settings

LOCK_NAME = ".running.lock"


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class BusyError(RuntimeError):
    pass


class JobLock:
    def __init__(self, settings: Settings) -> None:
        self.path = jobs_dir(settings) / LOCK_NAME

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _stale(self) -> bool:
        data = self.read()
        if data is None:
            return True
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return True
        return pid <= 0 or not psutil.pid_exists(pid)

    def occupied(self) -> dict[str, Any] | None:
        """活着的全局锁；过期锁会清掉。"""
        if not self.path.is_file():
            return None
        if self._stale():
            self.path.unlink(missing_ok=True)
            return None
        return self.read()

    def acquire(self, job_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file() and self._stale():
            self.path.unlink(missing_ok=True)
        payload = json.dumps(
            {"job_id": job_id, "pid": os.getpid(), "acquired_at": utcnow()},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            data = self.read() or {}
            raise BusyError(f"已有任务在跑：{data.get('job_id') or self.path}") from exc
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    def release(self, job_id: str) -> None:
        data = self.read()
        if data is None:
            return
        if data.get("job_id") == job_id:
            self.path.unlink(missing_ok=True)

    @contextmanager
    def hold(self, job_id: str) -> Iterator[None]:
        self.acquire(job_id)
        try:
            yield
        finally:
            self.release(job_id)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
