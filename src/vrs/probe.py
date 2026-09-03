from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


def which_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ProbeError("PATH 中没有 ffmpeg")
    return exe


def which_ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise ProbeError("PATH 中没有 ffprobe")
    return exe


def probe_video(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProbeError(f"视频文件无效：{path}")
    completed = subprocess.run(
        [
            which_ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ProbeError(completed.stderr.strip() or "ffprobe 失败")
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    if not has_video:
        raise ProbeError("没有视频流")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise ProbeError("时长无效")
    video = next(s for s in streams if s.get("codec_type") == "video")
    return {
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "format": (payload.get("format") or {}).get("format_name"),
        "size": int((payload.get("format") or {}).get("size") or path.stat().st_size),
        "path": str(path),
    }
