from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from vrs.jobstore import mark_stage, save_status
from vrs.probe import ProbeError, probe_video, which_ffmpeg
from vrs.settings import Settings

VIDEO_NAME = "video.mp4"


class DownloadError(RuntimeError):
    pass


def _log_path(directory: Path) -> Path:
    return directory / "logs" / "download.log"


def _append_log(directory: Path, text: str) -> None:
    path = _log_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def _ytdlp_bin() -> str:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise DownloadError("PATH 中没有 yt-dlp")
    return exe


def _already_done(directory: Path, job: dict[str, Any]) -> bool:
    dest = directory / "source" / VIDEO_NAME
    if job.get("stages", {}).get("download", {}).get("status") != "done":
        return False
    try:
        probe_video(dest)
        return True
    except ProbeError:
        return False


def _remux_or_copy(src: Path, dest: Path, directory: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return
    if src.suffix.lower() in {".mp4", ".m4v"}:
        shutil.copy2(src, dest)
        return
    cmd = [
        which_ffmpeg(),
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _append_log(directory, completed.stderr or completed.stdout or "")
    if completed.returncode == 0 and dest.is_file():
        return
    dest.unlink(missing_ok=True)
    cmd = [
        which_ffmpeg(),
        "-y",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _append_log(directory, completed.stderr or completed.stdout or "")
    if completed.returncode != 0 or not dest.is_file():
        raise DownloadError("ffmpeg 无法写成 mp4")


def _copy_local(job: dict[str, Any], directory: Path) -> None:
    original = Path(job["source"]["original_path"]).expanduser()
    if not original.is_absolute():
        raise DownloadError("本地进料必须是绝对路径")
    if not original.is_file():
        raise DownloadError(f"找不到文件：{original}")
    dest = directory / "source" / VIDEO_NAME
    _remux_or_copy(original, dest, directory)
    meta = {
        "kind": "file",
        "original_path": str(original),
        "original_size": original.stat().st_size,
        "original_name": original.name,
    }
    (directory / "source" / "ingest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _ytdlp_args(
    settings: Settings,
    url: str,
    directory: Path,
    *,
    cookies_override: Path | None = None,
) -> list[str]:
    cfg = settings.default.get("ytdlp") or {}
    source_dir = directory / "source"
    args = [
        _ytdlp_bin(),
        "--no-playlist",
        "--continue",
        "--no-progress",
        "--newline",
        "--write-info-json",
        "--merge-output-format",
        str(cfg.get("merge_output_format") or "mp4"),
        "-f",
        str(cfg.get("format") or "b[height<=1080]"),
        "-o",
        str(source_dir / "media.%(ext)s"),
        "--no-write-comments",
        url,
    ]
    override = cookies_override if cookies_override and cookies_override.is_file() else None
    cookies_file = str(cfg.get("cookies_file") or "").strip()
    browser = str(cfg.get("cookies_from_browser") or "").strip()
    if override:
        args[1:1] = ["--cookies", str(override)]
    elif cookies_file:
        cookies_path = Path(cookies_file).expanduser()
        if not cookies_path.is_file():
            raise DownloadError(f"cookies 文件不存在：{cookies_path}")
        args[1:1] = ["--cookies", str(cookies_path)]
    elif browser:
        args[1:1] = ["--cookies-from-browser", browser]
    return args


def _needs_cookies(stderr: str) -> bool:
    low = (stderr or "").lower()
    return "cookie" in low and ("need" in low or "needed" in low or "fresh cookies" in low)


def _exec_ytdlp(args: list[str], directory: Path) -> subprocess.CompletedProcess[str]:
    _append_log(directory, " ".join(args))
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(directory),
    )
    _append_log(directory, completed.stdout or "")
    _append_log(directory, completed.stderr or "")
    return completed


def _finalize_ytdlp(directory: Path) -> None:
    source = directory / "source"
    dest = source / VIDEO_NAME
    mp4s = [p for p in source.glob("media.*") if p.suffix.lower() == ".mp4" and p.is_file()]
    if not mp4s:
        mp4s = [p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"]
    if not mp4s:
        raise DownloadError("yt-dlp 没有产出 mp4")
    chosen = max(mp4s, key=lambda p: p.stat().st_size)
    if chosen.resolve() != dest.resolve():
        chosen.replace(dest)
    info_files = list(source.glob("*.info.json"))
    if info_files:
        target = source / "ytdlp.json"
        info_files[0].replace(target)


def _warmup_cookies(
    settings: Settings,
    url: str,
    directory: Path,
    *,
    download_to: Path | None = None,
) -> None:
    from vrs.stages.pagemeta import collect_for_url

    try:
        collect_for_url(settings, url, directory, download_to=download_to)
    except Exception as exc:  # noqa: BLE001
        _append_log(directory, f"browser cookie warmup failed: {type(exc).__name__}: {exc}")


def _media_ready(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 100_000


def _download_url(settings: Settings, job: dict[str, Any], directory: Path) -> None:
    from vrs.browser import needs_browser_cookies
    from vrs.f2douyin import DouyinIngestError, download_douyin, is_douyin_url

    url = job["source"].get("url")
    if not url:
        raise DownloadError("缺少 url")
    dest = directory / "source" / VIDEO_NAME
    if dest.is_file():
        try:
            probe_video(dest)
            if not is_douyin_url(url) or (directory / "source" / "page_meta.json").is_file():
                return
        except ProbeError:
            dest.unlink(missing_ok=True)
    if is_douyin_url(url):
        try:
            meta = download_douyin(settings, url, directory / "source")
        except DouyinIngestError as exc:
            raise DownloadError(str(exc)) from exc
        except Exception as exc:
            _append_log(directory, f"f2 douyin crash: {type(exc).__name__}: {exc}")
            raise DownloadError(f"抖音进料异常：{type(exc).__name__}: {exc}") from exc
        _append_log(
            directory,
            f"f2 douyin aweme_id={meta.get('aweme_id')} author={meta.get('author')}",
        )
        return
    auto_cookies = directory / "source" / "cookies.txt"
    media_tmp = directory / "source" / "media.mp4"
    if needs_browser_cookies(url):
        _warmup_cookies(settings, url, directory, download_to=media_tmp)
        if _media_ready(media_tmp):
            _append_log(directory, f"downloaded via play_addr {media_tmp.stat().st_size} bytes")
            _finalize_ytdlp(directory)
            return
    args = _ytdlp_args(
        settings,
        url,
        directory,
        cookies_override=auto_cookies if auto_cookies.is_file() else None,
    )
    completed = _exec_ytdlp(args, directory)
    if completed.returncode != 0 and _needs_cookies(completed.stderr or "") and "--cookies" not in args:
        _warmup_cookies(settings, url, directory, download_to=media_tmp)
        if _media_ready(media_tmp):
            _append_log(directory, f"downloaded via play_addr {media_tmp.stat().st_size} bytes")
            _finalize_ytdlp(directory)
            return
        if not auto_cookies.is_file():
            raise DownloadError((completed.stderr or "").strip() or "需要 cookies 但未能写出")
        args = _ytdlp_args(settings, url, directory, cookies_override=auto_cookies)
        completed = _exec_ytdlp(args, directory)
    if completed.returncode != 0:
        raise DownloadError((completed.stderr or "").strip() or (completed.stdout or "").strip() or "yt-dlp 失败")
    _finalize_ytdlp(directory)


def run_download(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    if _already_done(directory, job):
        return job
    mark_stage(job, directory, "download", "running")
    try:
        kind = job["source"]["kind"]
        if kind == "file":
            _copy_local(job, directory)
        elif kind == "url":
            _download_url(settings, job, directory)
        else:
            raise DownloadError(f"未知进料类型：{kind}")
        dest = directory / "source" / VIDEO_NAME
        info = probe_video(dest)
        job["source"]["probe"] = info
        mark_stage(job, directory, "download", "done")
        job["state"] = "paused"
        job["stage"] = "pagemeta"
        job["note"] = "阶段 1 完成"
        save_status(job, directory)
        return job
    except (DownloadError, ProbeError) as exc:
        mark_stage(job, directory, "download", "failed", error=str(exc))
        raise
