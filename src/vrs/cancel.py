"""任务放弃：打断 Comfy，阶段之间也能停。"""

from __future__ import annotations

from pathlib import Path


CANCEL_NAME = ".cancel"


class JobCancelled(RuntimeError):
    pass


def cancel_flag(directory: Path) -> Path:
    return directory / CANCEL_NAME


def request_cancel(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    cancel_flag(directory).write_text("1", encoding="utf-8")


def clear_cancel(directory: Path) -> None:
    cancel_flag(directory).unlink(missing_ok=True)


def cancel_requested(directory: Path) -> bool:
    return cancel_flag(directory).is_file()


def raise_if_cancelled(directory: Path) -> None:
    if cancel_requested(directory):
        raise JobCancelled("用户放弃")
