from __future__ import annotations

import subprocess
from pathlib import Path

from vrs.probe import ProbeError, which_ffmpeg


def run_ffmpeg(args: list[str], *, log_path: Path | None = None) -> None:
    cmd = [which_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *args]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(" ".join(cmd) + "\n")
            if completed.stderr:
                handle.write(completed.stderr + "\n")
    if completed.returncode != 0:
        raise ProbeError((completed.stderr or completed.stdout or "ffmpeg 失败").strip())


def extract_wav(video: Path, dest: Path, *, log_path: Path | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(dest)],
        log_path=log_path,
    )


def extract_fps_frames(video: Path, dest_dir: Path, fps: float, *, log_path: Path | None = None) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    pattern = dest_dir / "frame_%06d.jpg"
    run_ffmpeg(
        ["-i", str(video), "-vf", f"fps={fps}", "-q:v", "3", str(pattern)],
        log_path=log_path,
    )
    return sorted(dest_dir.glob("frame_*.jpg"))


def extract_frame_at(video: Path, dest: Path, seconds: float, *, log_path: Path | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-ss", f"{max(0.0, seconds):.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(dest)],
        log_path=log_path,
    )


def concat_videos(paths: list[Path], dest: Path, *, log_path: Path | None = None) -> None:
    if not paths:
        raise ProbeError("没有可拼接的片段")
    dest.parent.mkdir(parents=True, exist_ok=True)
    lst = dest.with_suffix(dest.suffix + ".concat.txt")
    lines = []
    for path in paths:
        if not path.is_file():
            raise ProbeError(f"缺片段 {path}")
        escaped = str(path.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            log_path=log_path,
        )
    except ProbeError:
        dest.unlink(missing_ok=True)
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            log_path=log_path,
        )


def trim_duration(video: Path, dest: Path, seconds: float, *, log_path: Path | None = None) -> None:
    """从头裁到指定秒数。H3 下限补出来的「保持」不要进合剪。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.05, float(seconds))
    try:
        run_ffmpeg(
            [
                "-i",
                str(video),
                "-t",
                f"{duration:.3f}",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            log_path=log_path,
        )
    except ProbeError:
        dest.unlink(missing_ok=True)
        run_ffmpeg(
            [
                "-i",
                str(video),
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            log_path=log_path,
        )


def burn_ass(video: Path, ass: Path, dest: Path, *, log_path: Path | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    escaped = str(ass.resolve()).replace("\\", "/").replace(":", r"\:")
    run_ffmpeg(
        [
            "-i",
            str(video),
            "-vf",
            f"ass='{escaped}'",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        log_path=log_path,
    )


def cut_clip(
    video: Path,
    dest: Path,
    t0: float,
    t1: float,
    *,
    log_path: Path | None = None,
    keep_audio: bool = False,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.05, t1 - t0)
    command = [
        "-ss",
        f"{max(0.0, t0):.3f}",
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
    ]
    if keep_audio:
        command += ["-c:a", "aac"]
    else:
        command.append("-an")
    command += ["-movflags", "+faststart", str(dest)]
    run_ffmpeg(command, log_path=log_path)
