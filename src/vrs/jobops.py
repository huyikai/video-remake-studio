"""WebUI 写脚本、设置、删任务、画幅确认。不负责跑流水线。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from vrs.aspect import aspect_mismatch
from vrs.cancel import request_cancel
from vrs.h3grid import normalize_generate_path, snap_seconds
from vrs.jobstore import get_job, save_status
from vrs.lock import JobLock, atomic_write_json
from vrs.passb import assemble_txt
from vrs.probe import ProbeError, probe_video
from vrs.settings import Settings, load_yaml
from vrs.stages.precheck import audit_job, run_precheck


class JobOpsError(RuntimeError):
    pass


def probe_local_file(path: str) -> dict[str, Any]:
    file_path = Path(path).expanduser()
    if not file_path.is_absolute():
        raise JobOpsError("本地进料必须是绝对路径")
    if not file_path.is_file():
        raise JobOpsError(f"找不到文件：{file_path}")
    try:
        probe = probe_video(file_path)
    except ProbeError as exc:
        raise JobOpsError(str(exc)) from exc
    default = "16:9"
    label, mismatch = aspect_mismatch(probe.get("width"), probe.get("height"), default)
    return {
        "path": str(file_path),
        "duration": probe.get("duration"),
        "width": probe.get("width"),
        "height": probe.get("height"),
        "source_aspect": label,
        "default_aspect": default,
        "mismatch": mismatch,
    }


def confirm_aspect(settings: Settings, job_id: str, *, follow_source: bool) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    default = str(settings.default.get("aspect_ratio") or "16:9")
    source = str(job.get("source_aspect") or default)
    options = job.setdefault("options", {})
    options["aspect_ratio"] = source if follow_source else default
    options["aspect_confirmed"] = True
    job["need_aspect_confirm"] = False
    job["note"] = f"输出画幅 {options['aspect_ratio']}，resume 继续"
    save_status(job, directory)
    return job


def _write_prompt_files(
    directory: Path,
    clip: dict[str, Any],
    doc: dict[str, Any],
    path: str,
    *,
    review_md: str | None = None,
) -> None:
    clip_id = str(clip["id"])
    doc = dict(doc)
    doc["clip_id"] = clip_id
    txt = assemble_txt(doc, clip, path)
    prompts = directory / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    atomic_write_json(prompts / f"{clip_id}.json", doc)
    (prompts / f"{clip_id}.txt").write_text(txt, encoding="utf-8")
    if review_md is not None:
        (prompts / f"{clip_id}.md").write_text(review_md, encoding="utf-8")
    index_path = directory / "prompts.json"
    index: dict[str, Any] = {}
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            index = {}
    items = list(index.get("prompts") or [])
    found = False
    for item in items:
        if str(item.get("clip_id")) == clip_id:
            item["prompt"] = f"prompts/{clip_id}.txt"
            item["review"] = f"prompts/{clip_id}.md"
            found = True
            break
    if not found:
        items.append(
            {
                "clip_id": clip_id,
                "prompt": f"prompts/{clip_id}.txt",
                "review": f"prompts/{clip_id}.md",
            }
        )
    index["prompts"] = items
    index.setdefault("generate_path", path)
    atomic_write_json(index_path, index)


def _after_save(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    if (job.get("stages") or {}).get("script", {}).get("status") != "done":
        audit = audit_job(directory)
        return {"ok": audit["ok"], "precheck": audit, "note": "脚本阶段未完成，只做静态检查"}
    try:
        job = run_precheck(settings, job, directory)
        audit = audit_job(directory)
        return {
            "ok": True,
            "precheck": audit,
            "job": {"state": job.get("state"), "stage": job.get("stage"), "note": job.get("note")},
        }
    except Exception as exc:
        return {"ok": False, "precheck": audit_job(directory), "error": str(exc)}


def save_clip_script(
    settings: Settings,
    job_id: str,
    clip_id: str,
    *,
    prompt_json: dict[str, Any] | None = None,
    prompt_txt: str | None = None,
    review_md: str | None = None,
    h3_seconds: float | None = None,
) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    clips_path = directory / "clips.json"
    if not clips_path.is_file():
        raise JobOpsError("还没有 clips.json")
    clips_doc = json.loads(clips_path.read_text(encoding="utf-8"))
    clips = list(clips_doc.get("clips") or [])
    clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
    if clip is None:
        raise JobOpsError(f"没有这一段：{clip_id}")
    path = normalize_generate_path(
        clips_doc.get("generate_path") or (job.get("options") or {}).get("generate_path") or "t2va_turbo"
    )
    if h3_seconds is not None:
        frames, seconds = snap_seconds(float(h3_seconds), settings)
        clip["h3_frames"] = frames
        clip["h3_seconds"] = seconds
        src = float(clip.get("source_seconds") or 0)
        clip["drift"] = round(seconds - src, 3)
        clip["padded"] = seconds > src + 0.05
        atomic_write_json(clips_path, clips_doc)
    json_path = directory / "prompts" / f"{clip_id}.json"
    if prompt_json is not None:
        _write_prompt_files(directory, clip, prompt_json, path, review_md=review_md)
    elif json_path.is_file() and prompt_txt is None:
        doc = json.loads(json_path.read_text(encoding="utf-8"))
        _write_prompt_files(directory, clip, doc, path, review_md=review_md)
    elif prompt_txt is not None:
        (directory / "prompts").mkdir(parents=True, exist_ok=True)
        (directory / "prompts" / f"{clip_id}.txt").write_text(prompt_txt, encoding="utf-8")
        if review_md is not None:
            (directory / "prompts" / f"{clip_id}.md").write_text(review_md, encoding="utf-8")
    else:
        raise JobOpsError("没有可保存的提示词")
    return _after_save(settings, job, directory)


def save_all_json(
    settings: Settings,
    job_id: str,
    *,
    clips_doc: dict[str, Any] | None = None,
    by_clip: dict[str, Any] | None = None,
) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    if clips_doc is not None:
        atomic_write_json(directory / "clips.json", clips_doc)
    clips_now = {}
    if (directory / "clips.json").is_file():
        clips_now = json.loads((directory / "clips.json").read_text(encoding="utf-8"))
    path = normalize_generate_path(
        clips_now.get("generate_path") or (job.get("options") or {}).get("generate_path") or "t2va_turbo"
    )
    index = {str(c["id"]): c for c in clips_now.get("clips") or []}
    for clip_id, doc in (by_clip or {}).items():
        clip = index.get(str(clip_id))
        if clip is None or not isinstance(doc, dict):
            raise JobOpsError(f"全脚本里有未知段 {clip_id}")
        _write_prompt_files(directory, clip, doc, path)
    return _after_save(settings, job, directory)


def delete_job(settings: Settings, job_id: str) -> None:
    directory, _job = get_job(settings, job_id)
    lock = JobLock(settings)
    occ = lock.occupied()
    if occ and occ.get("job_id") == job_id:
        request_cancel(directory)
        try:
            from vrs.comfyclient import interrupt

            interrupt(settings)
        except Exception:
            pass
    shutil.rmtree(directory, ignore_errors=True)


def settings_public(settings: Settings) -> dict[str, Any]:
    smtp = settings.smtp or {}
    comfy = settings.providers.get("comfy") or {}
    vl = settings.providers.get("vl") or {}
    return {
        "mode": settings.mode(),
        "mock_speed": settings.mock_speed(),
        "mock_faults": settings.default.get("mock_faults") or {},
        "bind_host": settings.bind_host(),
        "bind_port": settings.bind_port(),
        "comfy_base_url": str(comfy.get("base_url") or "http://127.0.0.1:8188"),
        "vl_kind": str(vl.get("kind") or "transformers"),
        "vl_model": str(vl.get("model") or ""),
        "gpu_memory_gb": settings.h3.get("gpu_memory_gb"),
        "hang_timeout_sec": settings.default.get("hang_timeout_sec"),
        "generate_clip_timeout_sec": settings.default.get("generate_clip_timeout_sec"),
        "clip_restart_max": settings.default.get("clip_restart_max"),
        "job_restart_max": settings.default.get("job_restart_max"),
        "aspect_ratio": settings.default.get("aspect_ratio"),
        "aspect_confirm": settings.default.get("aspect_confirm"),
        "review_mode": settings.default.get("review_mode"),
        "vl_mode": settings.default.get("vl_mode"),
        "esrgan": settings.default.get("esrgan"),
        "ass_burn": settings.default.get("ass_burn"),
        "smtp_enabled": bool(smtp.get("enabled")),
        "smtp_to": list(smtp.get("to") or []),
        "smtp_host": smtp.get("host") or "",
        "smtp_has_password": bool(smtp.get("password")),
        "smtp_hint": "密码和账号请改 config/smtp.local.yaml 或环境变量 VRS_SMTP_*，页面不回显。",
    }


def patch_settings(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"password", "api_key", "cookie", "smtp_password"}
    if any(k in body for k in forbidden):
        raise JobOpsError("密码类字段不能从页面提交")
    path = settings.root / "config" / "local.yaml"
    current = load_yaml(path) if path.is_file() else {}
    runtime_path = settings.root / "data" / "runtime.yaml"
    runtime = load_yaml(runtime_path) if runtime_path.is_file() else {}
    if "mode" in body and body["mode"] is not None:
        runtime["mode"] = str(body["mode"]).lower()
    if "mock_speed" in body and body["mock_speed"] is not None:
        runtime["mock_speed"] = str(body["mock_speed"])
    if "mock_faults" in body and body["mock_faults"] is not None:
        runtime["mock_faults"] = body["mock_faults"]
    mapping_default: dict[str, type] = {
        "hang_timeout_sec": int,
        "generate_clip_timeout_sec": int,
        "clip_restart_max": int,
        "job_restart_max": int,
        "aspect_ratio": str,
        "aspect_confirm": bool,
        "review_mode": str,
        "vl_mode": str,
        "esrgan": bool,
        "ass_burn": bool,
    }
    for key, caster in mapping_default.items():
        if key in body and body[key] is not None:
            current[key] = caster(body[key])
    if "mode" in body and str(body["mode"]).lower() not in {"mock", "real"}:
        raise JobOpsError("运行模式只能是 mock 或 real")
    if "mock_speed" in body and str(body["mock_speed"]) not in {"0.25x", "1x", "4x"}:
        raise JobOpsError("Mock 速度只能是 0.25x、1x 或 4x")
    if body.get("comfy_base_url"):
        providers = dict(current.get("providers") or {})
        comfy = dict(providers.get("comfy") or {})
        comfy["base_url"] = str(body["comfy_base_url"]).rstrip("/")
        providers["comfy"] = comfy
        current["providers"] = providers
    if body.get("gpu_memory_gb") is not None:
        h3 = dict(current.get("h3") or {})
        h3["gpu_memory_gb"] = float(body["gpu_memory_gb"])
        current["h3"] = h3
    smtp = dict(current.get("smtp") or {})
    if body.get("smtp_enabled") is not None:
        smtp["enabled"] = bool(body["smtp_enabled"])
    if "smtp_to" in body and body["smtp_to"] is not None:
        raw = body["smtp_to"]
        if isinstance(raw, str):
            smtp["to"] = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            smtp["to"] = [str(p).strip() for p in raw if str(p).strip()]
    if smtp:
        current["smtp"] = smtp
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if runtime:
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_path.write_text(
            yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    settings.reload()
    return settings_public(settings)
