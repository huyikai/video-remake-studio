from __future__ import annotations

from pathlib import Path

from vrs.settings import Settings, repo_root


def is_inside_repo(path: Path, root: Path | None = None) -> bool:
    root = (root or repo_root()).resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def jobs_dir(settings: Settings) -> Path:
    return settings.root / "data" / "jobs"


def assert_outside_repo(label: str, path: Path, settings: Settings) -> str | None:
    if is_inside_repo(path, settings.root):
        return f"{label} 落在仓库内：{path}"
    return None
