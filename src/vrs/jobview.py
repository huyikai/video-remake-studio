"""WebUI / Skill 用的任务详情：clip 状态、媒体路径、看门狗事件。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vrs.aspect import aspect_label
from vrs.h3grid import normalize_generate_path
from vrs.jobstore import get_job, iter_jobs, job_dir
from vrs.lock import JobLock
from vrs.passb import assemble_zh
from vrs.promptcheck import iter_clip_speech
from vrs.settings import Settings
from vrs.stages.generate import clip_output_dir, job_generate_path, quality_complete


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _exists(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def resolve_job_file(directory: Path, rel: str) -> Path:
    raw = (rel or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or ":" in raw:
        raise ValueError("非法路径")
    parts = Path(raw).parts
    if ".." in parts or parts[:1] == ("",):
        raise ValueError("非法路径")
    root = directory.resolve()
    path = (directory / raw).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(raw)
    return path


def elapsed_seconds(job: dict[str, Any]) -> float | None:
    start = job.get("created_at")
    end = job.get("updated_at")
    if job.get("state") == "running":
        end = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not start:
        return None
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end or start).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (b - a).total_seconds())


def occupied_job_id(settings: Settings) -> str | None:
    data = JobLock(settings).occupied()
    if not data:
        return None
    return str(data.get("job_id") or "") or None


def our_comfy_context(settings: Settings) -> tuple[set[str], str | None, str | None]:
    job_id = occupied_job_id(settings)
    if not job_id:
        return set(), None, None
    try:
        directory, job = get_job(settings, job_id)
    except FileNotFoundError:
        return set(), None, None
    progress = _load(directory / "generate.json") or {}
    ids: set[str] = set()
    if progress.get("last_prompt_id"):
        ids.add(str(progress["last_prompt_id"]))
    for clip_rec in (progress.get("clips") or {}).values():
        if not isinstance(clip_rec, dict):
            continue
        for quality_rec in clip_rec.values():
            if isinstance(quality_rec, dict) and quality_rec.get("prompt_id"):
                ids.add(str(quality_rec["prompt_id"]))
    path = None
    try:
        path = job_generate_path(directory, job)
        workflow = str((settings.h3.get(path) or {}).get("workflow") or "") or None
    except Exception:
        workflow = None
    quality = progress.get("last_quality")
    return ids, workflow, str(quality) if quality else None


def _clip_quality_status(
    directory: Path,
    clip: dict[str, Any],
    path: str,
    quality: str,
    progress: dict[str, Any],
) -> dict[str, Any]:
    clip_id = str(clip["id"])
    dest = clip_output_dir(directory, path, quality) / f"{clip_id}.mp4"
    rec = ((progress.get("clips") or {}).get(clip_id) or {}).get(quality) or {}
    prompt = directory / "prompts" / f"{clip_id}.txt"
    dirty = _exists(dest) and _exists(prompt) and _mtime(prompt) > _mtime(dest) + 0.5
    status = rec.get("status")
    if _exists(dest):
        status = "done"
    elif status not in {"running", "error"}:
        status = "pending"
    return {
        "status": status,
        "file": f"generate/{path}/{quality}/{clip_id}.mp4" if _exists(dest) else None,
        "error": rec.get("error"),
        "dirty": dirty,
        "prompt_id": rec.get("prompt_id"),
        "attempts": rec.get("attempts"),
    }


def _media(directory: Path, job: dict[str, Any], path: str) -> dict[str, str | None]:
    source = job.get("source") or {}
    video = str(source.get("video") or "source/video.mp4")
    cover = None
    for name in ("cover.jpg", "cover.png", f"output/{path}/cover.jpg"):
        if _exists(directory / name):
            cover = name
            break
    out = directory / "output" / path
    finish_done = str(((job.get("stages") or {}).get("finish") or {}).get("status") or "") == "done"
    return {
        "source": video if _exists(directory / video) else None,
        "draft": f"output/{path}/draft.mp4" if _exists(out / "draft.mp4") else None,
        "final": f"output/{path}/final.mp4" if finish_done and _exists(out / "final.mp4") else None,
        "cover": cover if finish_done else None,
        "ass": f"output/{path}/final.ass" if finish_done and _exists(out / "final.ass") else None,
    }


def _quality_flags(directory: Path, job: dict[str, Any]) -> tuple[bool, bool]:
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    if not clips:
        return False, False
    try:
        path = job_generate_path(directory, job)
    except Exception:
        return False, False
    return (
        quality_complete(directory, clips, "draft", path=path),
        quality_complete(directory, clips, "final", path=path),
    )


def _stage_status(job: dict[str, Any], name: str) -> str:
    rec = (job.get("stages") or {}).get(name)
    if isinstance(rec, dict):
        return str(rec.get("status") or "")
    return ""


def ui_stage(
    job: dict[str, Any],
    *,
    drafts_ready: bool = False,
    finals_ready: bool = False,
    last_quality: str | None = None,
) -> str:
    finish = _stage_status(job, "finish")
    generate = _stage_status(job, "generate")
    if finish in {"running", "done"}:
        return "finish"
    if generate == "running":
        return "clips" if last_quality == "final" or drafts_ready else "draft"
    if generate in {"failed", "waiting"}:
        return "clips" if drafts_ready else "draft"
    for name in ("download", "pagemeta", "understand", "script", "precheck"):
        if _stage_status(job, name) not in {"done", "skipped"}:
            return name
    if finals_ready:
        return "clips"
    if drafts_ready or _stage_status(job, "precheck") in {"done", "skipped"}:
        return "draft"
    backend = str(job.get("stage") or "download")
    if backend == "generate":
        return "draft"
    if backend == "finish":
        return "finish"
    return backend


def next_primary_action(
    job: dict[str, Any],
    *,
    dirty: bool = False,
    running: bool = False,
    drafts_ready: bool = False,
    finals_ready: bool = False,
) -> str:
    state = str(job.get("state") or "").lower()
    if running:
        return "busy"
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state in {"failed", "error"}:
        return "retry"
    if dirty:
        return "redraft"
    if state == "done":
        return "done"
    for name in ("download", "pagemeta", "understand", "script", "precheck"):
        if _stage_status(job, name) not in {"done", "skipped"}:
            return name
    if not drafts_ready:
        return "draft"
    if not finals_ready:
        return "final"
    if _stage_status(job, "finish") not in {"done", "skipped"}:
        return "assemble"
    return "done"


def _dirty_clip_ids(directory: Path, job: dict[str, Any]) -> list[str]:
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    if not clips:
        return []
    try:
        path = job_generate_path(directory, job)
    except Exception:
        return []
    progress = _load(directory / "generate.json") or {}
    dirty: list[str] = []
    for clip in clips:
        draft = _clip_quality_status(directory, clip, path, "draft", progress)
        final = _clip_quality_status(directory, clip, path, "final", progress)
        if draft["dirty"] or final["dirty"]:
            dirty.append(str(clip["id"]))
    return dirty


def summarize_job(
    job: dict[str, Any],
    *,
    dirty: bool = False,
    running: bool = False,
    drafts_ready: bool = False,
    finals_ready: bool = False,
    last_quality: str | None = None,
) -> dict[str, Any]:
    understand = job.get("understand_progress") or {}
    sub_progress = understand.get("chip") if isinstance(understand, dict) else None
    return {
        "id": job.get("id"),
        "mode": (job.get("options") or {}).get("mode") or "real",
        "state": job.get("state"),
        "stage": ui_stage(job, drafts_ready=drafts_ready, finals_ready=finals_ready, last_quality=last_quality),
        "note": job.get("note"),
        "sub_progress": sub_progress,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "elapsed_sec": elapsed_seconds(job),
        "source": job.get("source"),
        "options": job.get("options"),
        "need_aspect_confirm": bool(job.get("need_aspect_confirm")),
        "drafts_ready": drafts_ready,
        "finals_ready": finals_ready,
        "next_action": next_primary_action(
            job, dirty=dirty, running=running, drafts_ready=drafts_ready, finals_ready=finals_ready
        ),
    }


def _download_auth_cookie(job: dict[str, Any]) -> bool:
    from vrs.f2douyin import is_douyin_auth_error, is_douyin_url

    url = str((job.get("source") or {}).get("url") or "")
    if not is_douyin_url(url):
        return False
    error = ((job.get("stages") or {}).get("download") or {}).get("error")
    return bool(error) and is_douyin_auth_error(error)


def _cookie_expired_flag(settings: Settings) -> bool:
    from vrs.f2douyin import cookie_expired

    return cookie_expired(settings)


def list_jobs_payload(settings: Settings) -> dict[str, Any]:
    running = occupied_job_id(settings)
    jobs: list[dict[str, Any]] = []
    for job in iter_jobs(settings):
        job_id = str(job.get("id") or "")
        directory = job_dir(settings, job_id) if job_id else None
        dirty = bool(directory and directory.is_dir() and _dirty_clip_ids(directory, job))
        drafts_ready = False
        finals_ready = False
        last_quality = None
        if directory and directory.is_dir():
            drafts_ready, finals_ready = _quality_flags(directory, job)
            last_quality = str((_load(directory / "generate.json") or {}).get("last_quality") or "") or None
        jobs.append(
            summarize_job(
                job,
                dirty=dirty,
                running=bool(job_id and running == job_id),
                drafts_ready=drafts_ready,
                finals_ready=finals_ready,
                last_quality=last_quality,
            )
        )
    return {"jobs": jobs, "running_job_id": running}


def clip_editor_payload(directory: Path, clip_id: str) -> dict[str, Any]:
    clips_doc = _load(directory / "clips.json") or {}
    clips = list(clips_doc.get("clips") or [])
    clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
    if clip is None:
        raise FileNotFoundError(clip_id)
    dialogue = _load(directory / "dialogue.json") or {}
    speech = []
    for item in iter_clip_speech(clip, dialogue, clips):
        speech.append(
            {
                "t0": item.get("t0"),
                "t1": item.get("t1"),
                "text": item.get("text"),
                "source": item.get("source"),
                "emotion": (item.get("vocal_emotion") or {}).get("label")
                if isinstance(item.get("vocal_emotion"), dict)
                else None,
            }
        )
    json_path = directory / "prompts" / f"{clip_id}.json"
    txt_path = directory / "prompts" / f"{clip_id}.txt"
    md_path = directory / "prompts" / f"{clip_id}.md"
    prompt_json = _load(json_path) if json_path.is_file() else None
    script_zh = ""
    if isinstance(prompt_json, dict):
        script_zh = assemble_zh(prompt_json, clip)
    return {
        "clip": clip,
        "speech": speech,
        "script_zh": script_zh,
        "prompt_json": prompt_json,
        "prompt_txt": txt_path.read_text(encoding="utf-8") if txt_path.is_file() else "",
        "review_md": md_path.read_text(encoding="utf-8") if md_path.is_file() else "",
        "has_json": json_path.is_file(),
        "has_txt": txt_path.is_file(),
    }


def job_detail(settings: Settings, job_id: str, *, compact: bool = False) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    clips_doc = _load(directory / "clips.json") or {}
    clips = list(clips_doc.get("clips") or [])
    progress = _load(directory / "generate.json") or {}
    precheck = _load(directory / "precheck.json") or {}
    try:
        path = job_generate_path(directory, job)
    except Exception:
        path = str((job.get("options") or {}).get("generate_path") or "t2va_turbo")
        try:
            path = normalize_generate_path(path)
        except ValueError:
            path = "t2va_turbo"
    rows = []
    dirty_ids: list[str] = []
    for clip in clips:
        draft = _clip_quality_status(directory, clip, path, "draft", progress)
        final = _clip_quality_status(directory, clip, path, "final", progress)
        if draft["dirty"] or final["dirty"]:
            dirty_ids.append(str(clip["id"]))
        clip_id = str(clip.get("id"))
        rows.append(
            {
                "id": clip.get("id"),
                "event_id": clip.get("event_id"),
                "t0": clip.get("t0"),
                "t1": clip.get("t1"),
                "source_seconds": clip.get("source_seconds"),
                "h3_seconds": clip.get("h3_seconds"),
                "h3_frames": clip.get("h3_frames"),
                "padded": clip.get("padded"),
                "cast_reset": clip.get("cast_reset"),
                "has_script": (directory / "prompts" / f"{clip_id}.txt").is_file(),
                "draft": draft,
                "final": final,
            }
        )
    probe = (job.get("source") or {}).get("probe") or {}
    source_aspect = job.get("source_aspect")
    if not source_aspect and probe.get("width") and probe.get("height"):
        source_aspect = aspect_label(int(probe["width"]), int(probe["height"]))
    finish_doc = _load(directory / "finish.json") or {}
    drafts_ready = quality_complete(directory, clips, "draft", path=path) if clips else False
    finals_ready = quality_complete(directory, clips, "final", path=path) if clips else False
    occupied = occupied_job_id(settings) == job_id
    view = ui_stage(
        job,
        drafts_ready=drafts_ready,
        finals_ready=finals_ready,
        last_quality=str(progress.get("last_quality") or "") or None,
    )
    return {
        **job,
        "mode": (job.get("options") or {}).get("mode") or "real",
        "model_trace": [] if compact else list(job.get("model_trace") or []),
        "elapsed_sec": elapsed_seconds(job),
        "generate_path": path,
        "clips": rows,
        "dirty_clip_ids": dirty_ids,
        "drafts_ready": drafts_ready,
        "finals_ready": finals_ready,
        "stage": view,
        "ui_stage": view,
        "next_action": next_primary_action(
            job,
            dirty=bool(dirty_ids),
            running=occupied,
            drafts_ready=drafts_ready,
            finals_ready=finals_ready,
        ),
        "media": _media(directory, job, path),
        "events": list(progress.get("events") or [])[-40:],
        "precheck": {
            "ok": precheck.get("ok"),
            "errors": precheck.get("errors") or [],
            "warnings": precheck.get("warnings") or [],
        },
        "finish_report": {
            "ok": finish_doc.get("ok"),
            "clips": finish_doc.get("clips"),
            "concat": bool(finish_doc.get("concat")),
            "ass_burn": bool(finish_doc.get("ass_burn")),
            "ass_events": finish_doc.get("ass_events") or 0,
            "cover": finish_doc.get("cover"),
        },
        "generate_progress": {
            "last_clip": progress.get("last_clip"),
            "last_quality": progress.get("last_quality"),
            "comfy_starts": progress.get("comfy_starts") or 0,
            "draft": progress.get("draft"),
            "final": progress.get("final"),
        },
        "source_aspect": source_aspect,
        "running": occupied,
        "download_auth_cookie": _download_auth_cookie(job),
        "douyin_cookie_from_env": bool((os.environ.get("VRS_DOUYIN_COOKIE") or "").strip()),
        "douyin_cookie_expired": _cookie_expired_flag(settings),
        "scripts": None,
        "clips_json": None if compact else (clips_doc if clips else None),
        "prompts_index": None if compact else _load(directory / "prompts.json"),
    }

