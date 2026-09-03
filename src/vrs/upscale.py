"""可选 RealESRGAN。没装、不是视频后端、或关掉配置时跳过，不失败。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable


def _log(log: Callable[[str], None] | None, text: str) -> None:
    if log:
        log(text)


def maybe_upscale(
    settings: Any,
    src: Path,
    dest: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> Path:
    if not bool(settings.default.get("esrgan")):
        return src
    root = settings.path("realesrgan_dir")
    exe = None
    if root.is_dir():
        for name in ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan", "realesrgan.exe"):
            hit = root / name
            if hit.is_file():
                exe = hit
                break
        if exe is None:
            found = shutil.which("realesrgan-ncnn-vulkan")
            exe = Path(found) if found else None
    if exe is None:
        _log(log, "ESRGAN 已开启但找不到 realesrgan-ncnn-vulkan，跳过超分")
        return src
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "-i", str(src), "-o", str(dest), "-n", "realesrgan-x4plus", "-s", "2"]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 or not dest.is_file():
        _log(log, f"ESRGAN 失败，用未超分成片：{(completed.stderr or completed.stdout or '')[:240]}")
        dest.unlink(missing_ok=True)
        return src
    _log(log, f"ESRGAN 写出 {dest}")
    return dest
