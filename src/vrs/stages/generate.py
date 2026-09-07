"""生成：把每段 H3 提示词交给 ComfyUI，写出 generate/{path}/{quality}/h3_NN.mp4。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from vrs.cancel import JobCancelled, cancel_requested, raise_if_cancelled
from vrs.comfyclient import (
    ComfyError,
    health,
    interrupt,
    load_workflow,
    looks_like_oom,
    object_info,
    queue_busy,
    run_h3_clip,
    start_comfy,
    wait_until_up,
)
from vrs.deliver import trim_and_concat
from vrs.envcheck import collect_env
from vrs.h3grid import PATH_KEYFRAMES, normalize_generate_path
from vrs.jobstore import mark_stage, only_clips, save_status
from vrs.lock import atomic_write_json, utcnow
from vrs.llmclient import unload_llm
from vrs.mailer import send_mail
from vrs.media import extract_frame_at
from vrs.probe import ProbeError, probe_video
from vrs.settings import Settings
from vrs.vlclient import unload_vl


class GenerateError(RuntimeError):
    pass


class GenerateWaiting(RuntimeError):
    pass


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "generate.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")
    print(text, flush=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _quality_params(
    settings: Settings, path: str, quality: str, job: dict[str, Any] | None = None
) -> dict[str, Any]:
    snap = ((job or {}).get("options") or {}).get("generate") or {}
    rec = snap.get(quality) if isinstance(snap, dict) else None
    if isinstance(rec, dict) and rec.get("workflow"):
        return {
            "workflow": str(rec["workflow"]),
            "megapixels": float(rec.get("megapixels") or 0.4),
            "steps": int(rec.get("steps") or 6),
            "sampler": rec.get("sampler"),
            "scheduler": rec.get("scheduler"),
            "ref_image_size": rec.get("ref_image_size"),
        }
    block = dict(settings.h3.get(path) or {})
    q = dict(block.get(quality) or {})
    if not q:
        raise GenerateError(f"{path} 没有 {quality} 参数")
    workflow = str(q.get("workflow") or block.get("workflow") or "")
    if not workflow:
        raise GenerateError(f"{path} 没有 workflow")
    return {
        "workflow": workflow,
        "megapixels": float(q.get("megapixels") or 0.4),
        "steps": int(q.get("steps") or 6),
        "sampler": q.get("sampler"),
        "scheduler": q.get("scheduler"),
        "ref_image_size": q.get("ref_image_size"),
    }


def _seed(job_id: str, clip_id: str, quality: str) -> int:
    digest = hashlib.md5(f"{job_id}:{clip_id}:{quality}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _clip_ready(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024:
        return False
    try:
        probe_video(path)
        return True
    except ProbeError:
        path.unlink(missing_ok=True)
        return False


def _ensure_keyframe(clip: dict[str, Any], job: dict[str, Any], directory: Path) -> Path:
    rel = str(clip.get("keyframe") or f"keyframes/{clip['id']}_a.jpg")
    dest = directory / rel
    if dest.is_file():
        return dest
    video = directory / str((job.get("source") or {}).get("video") or "source/video.mp4")
    extract_frame_at(video, dest, float(clip["t0"]), log_path=directory / "logs" / "generate.log")
    return dest


def _prompt_text(directory: Path, item: dict[str, Any], negative: str) -> str:
    del negative
    rel = str(item.get("prompt") or f"prompts/{item.get('clip_id')}.txt")
    path = directory / rel
    if not path.is_file():
        raise GenerateError(f"缺提示词 {rel}")
    # 不要把 subtitle/caption 写进 Avoid：H3 会当成要烧字幕
    return path.read_text(encoding="utf-8").strip()


def _clip_timeout(settings: Settings, seconds: float, steps: int) -> float:
    configured = float(settings.default.get("generate_clip_timeout_sec") or 1800)
    # 官方非 LoRA 成片时长接近帧数平方，13s 段实测超过 7000s 仍在算。
    scaled = float(seconds) * float(steps) * 45.0 + 600.0
    return max(configured, scaled, 3600.0)


def _boot_comfy(settings: Settings, directory: Path) -> None:
    ok, detail = health(settings)
    if ok:
        return
    _log(directory, f"Comfy 未就绪（{detail}），尝试启动")
    try:
        start_comfy(settings, log_path=directory / "logs" / "comfy-start.log")
    except ComfyError as exc:
        raise GenerateWaiting(str(exc)) from exc
    boot = float(settings.default.get("hang_timeout_sec") or 180)
    if not wait_until_up(settings, timeout=max(boot, 300.0)):
        raise GenerateWaiting("Comfy 启动超时，确认 minmaxH3/start.ps1 能跑起来后再 resume")
    _log(directory, "Comfy 已起来")


def _wait_foreign_queue(settings: Settings, directory: Path) -> None:
    limit = float(settings.default.get("hang_timeout_sec") or 180)
    waited = 0.0
    while queue_busy(settings) and waited < limit:
        raise_if_cancelled(directory)
        _log(directory, "Comfy 队列里还有别人的活，等它结束")
        time.sleep(5.0)
        waited += 5.0
    if queue_busy(settings):
        raise GenerateWaiting("Comfy 被外部占用，空出来后再 resume")


def job_generate_path(directory: Path, job: dict[str, Any] | None = None) -> str:
    prompts_doc = _load_json(directory / "prompts.json") or {}
    clips_doc = _load_json(directory / "clips.json") or {}
    opt = ((job or {}).get("options") or {}).get("generate_path")
    return normalize_generate_path(
        prompts_doc.get("generate_path") or clips_doc.get("generate_path") or opt or "i2va_turbo"
    )


def clip_output_dir(directory: Path, path: str, quality: str) -> Path:
    return directory / "generate" / path / quality


def quality_complete(
    directory: Path,
    clips: list[dict[str, Any]],
    quality: str,
    path: str | None = None,
) -> bool:
    path = normalize_generate_path(path) if path else job_generate_path(directory)
    root = clip_output_dir(directory, path, quality)
    return bool(clips) and all(_clip_ready(root / f"{c['id']}.mp4") for c in clips)


def next_quality(
    job: dict[str, Any],
    directory: Path,
    clips: list[dict[str, Any]],
    path: str | None = None,
) -> str:
    if not quality_complete(directory, clips, "draft", path=path):
        return "draft"
    return "final"


def _save_progress(directory: Path, doc: dict[str, Any]) -> None:
    atomic_write_json(directory / "generate.json", doc)


def _push_event(progress: dict[str, Any], kind: str, **fields: Any) -> None:
    events = list(progress.get("events") or [])
    events.append({"at": utcnow(), "kind": kind, **fields})
    progress["events"] = events[-50:]


def _concat_quality(
    directory: Path, clips: list[dict[str, Any]], quality: str, path: str
) -> str | None:
    dest_dir = clip_output_dir(directory, path, quality)
    paths = [dest_dir / f"{c['id']}.mp4" for c in clips]
    if not all(p.is_file() for p in paths):
        return None
    dest = directory / "output" / path / f"{quality}.mp4"
    trim_and_concat(
        clips,
        src_dir=dest_dir,
        dest=dest,
        work_dir=dest_dir / "trimmed",
        log_path=directory / "logs" / "generate.log",
    )
    return f"output/{path}/{quality}.mp4"


def run_generate(
    settings: Settings,
    job: dict[str, Any],
    directory: Path,
    *,
    quality: str | None = None,
) -> dict[str, Any]:
    if (job.get("stages") or {}).get("precheck", {}).get("status") != "done":
        raise GenerateError("预检还没过")
    clips_doc = _load_json(directory / "clips.json") or {}
    prompts_doc = _load_json(directory / "prompts.json") or {}
    clips = list(clips_doc.get("clips") or [])
    if not clips:
        raise GenerateError("缺少 clips.json")
    path = normalize_generate_path(
        (prompts_doc.get("generate_path") or (job.get("options") or {}).get("generate_path") or "i2va_turbo")
    )
    quality = quality or next_quality(job, directory, clips, path=path)
    params = _quality_params(settings, path, quality, job)
    env = collect_env(settings, stage="generate")
    missing = [i["detail"] for i in env["install"] if i["id"] == "h3_workflows" and not i["ok"]]
    if missing:
        raise GenerateWaiting("；".join(missing))

    unload_vl()
    unload_llm()
    mark_stage(job, directory, "generate", "running")
    progress = _load_json(directory / "generate.json") or {}
    if progress.get("generate_path") != path:
        if progress:
            _log(directory, f"生成路线改为 {path}，忽略上一路线的进度")
        progress = {"generate_path": path, "clips": {}}
    progress["generate_path"] = path
    progress.setdefault("clips", {})

    try:
        _boot_comfy(settings, directory)
        _wait_foreign_queue(settings, directory)
        info = object_info(settings, cache={})
        workflow = load_workflow(settings, str(params["workflow"]))
        negative = str(prompts_doc.get("negative_prompt") or settings.h3.get("negative_prompt") or "")
        prompt_index = {str(p.get("clip_id")): p for p in prompts_doc.get("prompts") or []}
        gpu_gb = float(settings.h3.get("gpu_memory_gb") or 16)
        low_vram = gpu_gb <= 8
        aspect = str(
            (job.get("options") or {}).get("aspect_ratio")
            or settings.default.get("aspect_ratio")
            or "16:9"
        )
        wants_image = bool(PATH_KEYFRAMES.get(path)) or path == "ref2va"
        clip_tries = int(settings.default.get("clip_restart_max") or 3)
        job_restarts = int(progress.get("comfy_starts") or 0)
        job_restart_max = int(settings.default.get("job_restart_max") or 10)

        dest_dir = clip_output_dir(directory, path, quality)
        dest_dir.mkdir(parents=True, exist_ok=True)
        done = 0
        skipped = 0
        rel_dir = f"generate/{path}/{quality}"
        for clip in clips:
            raise_if_cancelled(directory)
            clip_id = str(clip["id"])
            dest = dest_dir / f"{clip_id}.mp4"
            if _clip_ready(dest):
                skipped += 1
                rec = (progress["clips"].setdefault(clip_id, {})).setdefault(quality, {})
                rec.update({"status": "done", "file": f"{rel_dir}/{clip_id}.mp4"})
                continue
            item = prompt_index.get(clip_id)
            if item is None:
                raise GenerateError(f"{clip_id} 在 prompts.json 里没有")
            text = _prompt_text(directory, item, negative)
            seconds = float(clip["h3_seconds"])
            timeout = _clip_timeout(settings, seconds, int(params["steps"]))
            image_path = _ensure_keyframe(clip, job, directory) if wants_image else None
            prefix = f"vrs/{job['id']}/{path}/{quality}/{clip_id}"
            last_err: Exception | None = None
            for attempt in range(1, clip_tries + 1):
                try:
                    _log(
                        directory,
                        f"{clip_id} {quality} 第 {attempt}/{clip_tries} 次  "
                        f"{seconds:.2f}s {params['workflow']} steps={params['steps']} mp={params['megapixels']}",
                    )
                    prompt_id, _ = run_h3_clip(
                        settings,
                        workflow=workflow,
                        info=info,
                        prompt_text=text,
                        seconds=seconds,
                        steps=int(params["steps"]),
                        megapixels=float(params["megapixels"]),
                        aspect=aspect,
                        seed=_seed(str(job["id"]), clip_id, quality),
                        filename_prefix=prefix,
                        dest=dest,
                        timeout=timeout,
                        image_path=image_path,
                        image_subfolder=f"vrs/{job['id']}",
                        low_vram=low_vram,
                        sampler=str(params["sampler"]) if params.get("sampler") else None,
                        scheduler=str(params["scheduler"]) if params.get("scheduler") else None,
                        abort=lambda: cancel_requested(directory),
                    )
                    rec = (progress["clips"].setdefault(clip_id, {})).setdefault(quality, {})
                    rec.update(
                        {
                            "status": "done",
                            "file": f"{rel_dir}/{clip_id}.mp4",
                            "prompt_id": prompt_id,
                            "attempts": attempt,
                        }
                    )
                    progress["last_clip"] = clip_id
                    progress["last_quality"] = quality
                    progress["last_prompt_id"] = prompt_id
                    _push_event(progress, "clip_done", clip_id=clip_id, quality=quality)
                    _save_progress(directory, progress)
                    _log(directory, f"{clip_id} {quality} 完成 {dest.stat().st_size} bytes")
                    last_err = None
                    break
                except ComfyError as exc:
                    if cancel_requested(directory) or "已取消" in str(exc):
                        raise JobCancelled("用户放弃") from exc
                    last_err = exc
                    dest.unlink(missing_ok=True)
                    rec = (progress["clips"].setdefault(clip_id, {})).setdefault(quality, {})
                    rec.update({"status": "error", "error": str(exc), "attempts": attempt})
                    _save_progress(directory, progress)
                    _log(directory, f"{clip_id} {quality} 失败：{exc}")
                    interrupt(settings)
                    if looks_like_oom(str(exc)) and job_restarts < job_restart_max:
                        job_restarts += 1
                        progress["comfy_starts"] = job_restarts
                        _save_progress(directory, progress)
                        _log(directory, f"像是显存爆了，重启 Comfy（{job_restarts}/{job_restart_max}）")
                        _push_event(
                            progress,
                            "comfy_restart",
                            clip_id=clip_id,
                            quality=quality,
                            detail=f"第 {job_restarts} 次重启",
                        )
                        send_mail(
                            settings.smtp,
                            subject=f"VRS 重启 Comfy {job['id']}",
                            body=f"job {job['id']} 从 {clip_id} {quality} 续跑（第 {job_restarts} 次重启）",
                            log=lambda text: _log(directory, text),
                        )
                        try:
                            start_comfy(settings, log_path=directory / "logs" / "comfy-start.log")
                        except ComfyError:
                            pass
                        if not wait_until_up(settings, timeout=float(settings.default.get("hang_timeout_sec") or 180)):
                            raise GenerateWaiting("重启 Comfy 后还是连不上") from exc
                        info = object_info(settings, cache={})
            if last_err is not None:
                raise GenerateError(f"{clip_id} {quality} 重试 {clip_tries} 次仍失败：{last_err}") from last_err
            done += 1

        concat_rel = _concat_quality(directory, clips, quality, path)
        progress[quality] = {
            "status": "done",
            "concat": concat_rel,
            "clips": len(clips),
        }
        _save_progress(directory, progress)
        extra = f"，跳过已完成 {skipped}" if skipped else ""
        concat_note = f"，整片 {concat_rel}" if concat_rel else ""
        _log(directory, f"{quality} 完成 {done} 段{extra}{concat_note}")
        mark_stage(job, directory, "generate", "done")
        subset = only_clips(job)
        subset_note = f"（验证 {', '.join(subset)}）" if subset else ""
        if quality == "draft":
            job["state"] = "paused"
            job["stage"] = "finish"
            job["note"] = (
                f"试片 {len(clips)} 段已齐{subset_note}{concat_note}。"
                f"看 generate/{path}/draft/ 或 output/{path}/draft.mp4，出成片按同一脚本生成并交付"
            )
            if concat_rel:
                send_mail(
                    settings.smtp,
                    subject=f"VRS 试片待审 {job['id']}",
                    body=f"job {job['id']}\n{job['note']}\n本机 {(directory / concat_rel).resolve()}",
                    attachments=[directory / concat_rel],
                    log=lambda text: _log(directory, text),
                )
        else:
            job["state"] = "paused"
            job["stage"] = "finish"
            job["note"] = f"成片 {len(clips)} 段已齐{subset_note}{concat_note}；resume 拼接"
        save_status(job, directory)
        return job
    except JobCancelled:
        mark_stage(job, directory, "generate", "pending", error="用户放弃")
        job["state"] = "cancelled"
        job["note"] = "用户放弃"
        save_status(job, directory)
        raise
    except GenerateWaiting as exc:
        mark_stage(job, directory, "generate", "waiting", error=str(exc))
        job["state"] = "paused"
        job["note"] = str(exc)
        save_status(job, directory)
        return job
    except (GenerateError, ComfyError, ProbeError, ValueError, KeyError, TypeError, OSError) as exc:
        mark_stage(job, directory, "generate", "failed", error=str(exc))
        job["note"] = str(exc)
        save_status(job, directory)
        raise
