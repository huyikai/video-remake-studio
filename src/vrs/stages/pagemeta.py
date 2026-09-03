from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vrs.browser import visit_and_collect, write_cookies_file
from vrs.jobstore import mark_stage, save_status
from vrs.settings import Settings


def _find_aweme(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if "desc" in node and isinstance(node.get("statistics"), dict):
            return node
        for value in node.values():
            found = _find_aweme(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_aweme(value)
            if found:
                return found
    return None


def _compact_meta(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    extracted = payload.get("extracted") or {}
    aweme = extracted.get("aweme") or _find_aweme(extracted.get("render"))
    author = None
    stats: dict[str, Any] = {}
    desc = extracted.get("description")
    title = extracted.get("title")
    if aweme:
        desc = aweme.get("desc") or desc
        author_obj = aweme.get("author") or {}
        if isinstance(author_obj, dict):
            author = author_obj.get("nickname") or author_obj.get("unique_id")
        raw_stats = aweme.get("statistics") or {}
        if isinstance(raw_stats, dict):
            stats = {
                "like": raw_stats.get("digg_count"),
                "comment": raw_stats.get("comment_count"),
                "share": raw_stats.get("share_count"),
                "collect": raw_stats.get("collect_count"),
                "play": raw_stats.get("play_count"),
            }
    if not (title and str(title).strip()):
        title = desc
    return {
        "url": url,
        "final_url": payload.get("final_url"),
        "via": payload.get("via"),
        "title": title,
        "description": desc,
        "author": author,
        "stats": stats,
        "body_preview": (extracted.get("body_preview") or "")[:400],
        "skipped": False,
    }


def write_skipped(directory: Path, reason: str) -> None:
    path = directory / "source" / "page_meta.skipped.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"skipped": True, "reason": reason}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_meta(directory: Path, meta: dict[str, Any]) -> None:
    path = directory / "source" / "page_meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect_for_url(
    settings: Settings,
    url: str,
    directory: Path,
    *,
    download_to: Path | None = None,
) -> dict[str, Any]:
    """打开页面：写出 cookies.txt 和 page_meta.json。download_to 时顺带拉取播放地址。"""
    payload = visit_and_collect(settings, url, download_to=download_to)
    cookies = payload.get("cookies") or []
    if cookies:
        write_cookies_file(directory / "source" / "cookies.txt", cookies)
    meta = _compact_meta(url, payload)
    if payload.get("downloaded_bytes"):
        meta["downloaded_bytes"] = payload["downloaded_bytes"]
    if payload.get("download_error"):
        meta["download_error"] = payload["download_error"]
    write_meta(directory, meta)
    return meta


def run_pagemeta(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    status = job.get("stages", {}).get("pagemeta", {}).get("status")
    if status in {"done", "skipped"}:
        if (directory / "source" / "page_meta.json").is_file() or (
            directory / "source" / "page_meta.skipped.json"
        ).is_file():
            return job
    if job.get("source", {}).get("kind") != "url":
        write_skipped(directory, "本地文件不抓页面")
        mark_stage(job, directory, "pagemeta", "skipped")
        job["state"] = "paused"
        job["stage"] = "understand"
        job["note"] = "阶段 2 跳过（本地文件）"
        save_status(job, directory)
        return job

    if (directory / "source" / "page_meta.json").is_file():
        mark_stage(job, directory, "pagemeta", "done")
        job["state"] = "paused"
        job["stage"] = "understand"
        job["note"] = "阶段 2 完成（下载时已写 page_meta）"
        save_status(job, directory)
        return job

    url = job["source"].get("url") or ""
    from vrs.f2douyin import is_douyin_url

    if is_douyin_url(url):
        reason = "应由 f2 在 download 写出 page_meta.json"
        write_skipped(directory, reason)
        mark_stage(job, directory, "pagemeta", "skipped", error=reason)
        job["state"] = "paused"
        job["stage"] = "understand"
        job["note"] = f"阶段 2 已跳过：{reason}"
        save_status(job, directory)
        return job

    mark_stage(job, directory, "pagemeta", "running")
    try:
        collect_for_url(settings, url, directory)
        mark_stage(job, directory, "pagemeta", "done")
        job["state"] = "paused"
        job["stage"] = "understand"
        job["note"] = "阶段 2 完成"
        save_status(job, directory)
        return job
    except Exception as exc:  # noqa: BLE001 — 失败不阻断
        write_skipped(directory, str(exc))
        mark_stage(job, directory, "pagemeta", "skipped", error=str(exc))
        job["state"] = "paused"
        job["stage"] = "understand"
        job["note"] = f"阶段 2 已跳过：{exc}"
        save_status(job, directory)
        return job
