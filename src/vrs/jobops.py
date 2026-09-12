"""WebUI 写脚本、设置、删任务、画幅确认。不负责跑流水线。"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

import yaml

from vrs.aspect import aspect_mismatch
from vrs.cancel import request_cancel
from vrs.h3grid import merge_t2va_snapshot, normalize_generate_path, snap_seconds, t2va_defaults
from vrs.jobstore import get_job, iter_jobs, job_dir, save_status
from vrs.lock import BusyError, JobLock, atomic_write_json
from vrs.passb import assemble_md, assemble_txt, assemble_zh, clip_facts, rewrite_clip
from vrs.probe import ProbeError, probe_video
from vrs.settings import Settings, load_yaml
from vrs.stages.precheck import audit_job, run_precheck


class JobOpsError(RuntimeError):
    pass


def _reject_if_busy(settings: Settings) -> None:
    occ = JobLock(settings).occupied()
    if occ:
        raise BusyError(f"已有任务在跑：{occ.get('job_id')}，等它结束再改脚本")


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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _clip_facts_for(
    directory: Path, clip: dict[str, Any], clips: list[dict[str, Any]]
) -> dict[str, Any]:
    beats = _load_json(directory / "beats.json") or {}
    dialogue = _load_json(directory / "dialogue.json") or {}
    cuts = [float(c) for c in ((_load_json(directory / "scene_cuts.json") or {}).get("cuts") or [])]
    return clip_facts(clip, beats, dialogue, cuts, root=directory, clips=clips)


def _write_prompt_files(
    directory: Path,
    clip: dict[str, Any],
    doc: dict[str, Any],
    path: str,
    *,
    review_md: str | None = None,
    facts: dict[str, Any] | None = None,
) -> None:
    clip_id = str(clip["id"])
    doc = dict(doc)
    doc["clip_id"] = clip_id
    txt = assemble_txt(doc, clip, path)
    if review_md is None and facts is not None:
        review_md = assemble_md(doc, clip, facts, txt)
    prompts = directory / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    json_path = prompts / f"{clip_id}.json"
    txt_path = prompts / f"{clip_id}.txt"
    if not json_path.is_file() or json_path.read_text(encoding="utf-8") != json.dumps(doc, ensure_ascii=False, indent=2) + "\n":
        atomic_write_json(json_path, doc)
    if not txt_path.is_file() or txt_path.read_text(encoding="utf-8") != txt:
        txt_path.write_text(txt, encoding="utf-8")
    if review_md is not None:
        md_path = prompts / f"{clip_id}.md"
        if not md_path.is_file() or md_path.read_text(encoding="utf-8") != review_md:
            md_path.write_text(review_md, encoding="utf-8")
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
    # prompt_hash 不在 save 时回填：写 txt 之后回填会把"刚编辑的版本"误标为"video 用的版本"。
    # 老 clip 让 jobview 显示 unknown / 待重生成，等下次 generate.py 自然写入正确 hash。


def _after_save(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    if str(job.get("state") or "").lower() == "done":
        job["state"] = "paused"
        job["note"] = "脚本已改，该段待重新出试片"
        save_status(job, directory)
    if (job.get("stages") or {}).get("script", {}).get("status") != "done":
        audit = audit_job(directory)
        return {"ok": audit["ok"], "precheck": audit, "note": "脚本阶段未完成，只做静态检查"}
    try:
        job = run_precheck(settings, job, directory)
        if str(job.get("state") or "").lower() == "done":
            job["state"] = "paused"
            job["note"] = "脚本已改，该段待重新出试片"
            save_status(job, directory)
        audit = audit_job(directory)
        return {
            "ok": True,
            "precheck": audit,
            "job": {"state": job.get("state"), "stage": job.get("stage"), "note": job.get("note")},
        }
    except Exception as exc:
        return {"ok": False, "precheck": audit_job(directory), "error": str(exc)}


def _rewrite_or_save(
    settings: Settings,
    directory: Path,
    clip: dict[str, Any],
    clips: list[dict[str, Any]],
    path: str,
    *,
    script_zh: str | None,
    requirement: str | None,
    current: dict[str, Any],
) -> dict[str, Any]:
    """按中文诉求让 AI 重写英文脚本；失败保留旧版本并返回可读错误。"""
    log_path = directory / "logs" / "rewrite.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(text: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")

    facts = _clip_facts_for(directory, clip, clips)
    edit: dict[str, Any] = {"mode": "manual" if script_zh is not None else "ai"}
    if script_zh is not None:
        edit["script_zh"] = script_zh
    else:
        edit["requirement"] = requirement or ""
    try:
        doc, txt = rewrite_clip(
            settings, clip, facts, path=path, current=current, edit=edit, log=log
        )
    except Exception as exc:  # noqa: BLE001 — 保留旧版本，返回可读错误
        return {
            "ok": False,
            "error": f"AI 未生成有效英文脚本，请调整中文说明后重试：{exc}",
            "clip_id": str(clip["id"]),
        }
    # 生成成功才落盘，且顺序写回，保证不出现半更新文件
    _write_prompt_files(directory, clip, doc, path, facts=facts)
    return {"ok": True, "preview": {"prompt_txt": txt, "review_md": assemble_md(doc, clip, facts, txt), "prompt_json": doc}}


def save_clip_script(
    settings: Settings,
    job_id: str,
    clip_id: str,
    *,
    script_zh: str | None = None,
    requirement: str | None = None,
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
    json_path = directory / "prompts" / f"{clip_id}.json"
    current = _load_json(json_path) or {}
    if not isinstance(current, dict):
        raise JobOpsError(f"{clip_id}: prompts/{clip_id}.json 不是 JSON 对象")

    seconds_dirty = False
    if h3_seconds is not None:
        frames, seconds = snap_seconds(float(h3_seconds), settings)
        clip["h3_frames"] = frames
        clip["h3_seconds"] = seconds
        src = float(clip.get("source_seconds") or 0)
        clip["drift"] = round(seconds - src, 3)
        clip["padded"] = seconds > src + 0.05
        seconds_dirty = True

    if prompt_txt is not None and script_zh is None and requirement is None and prompt_json is None:
        raise JobOpsError(
            "不能直接提交英文提示词。请改中文脚本，程序会自动生成对应的英文 H3 脚本。"
        )

    if script_zh is not None or requirement is not None:
        _reject_if_busy(settings)
        result = _rewrite_or_save(
            settings,
            directory,
            clip,
            clips,
            path,
            script_zh=script_zh,
            requirement=requirement,
            current=current,
        )
        if not result.get("ok"):
            return {"ok": False, **result}
        if seconds_dirty:
            atomic_write_json(clips_path, clips_doc)
        return _after_save(settings, job, directory)

    if prompt_json is not None:
        _reject_if_busy(settings)
        facts = _clip_facts_for(directory, clip, clips)
        _write_prompt_files(directory, clip, prompt_json, path, facts=facts)
        if seconds_dirty:
            atomic_write_json(clips_path, clips_doc)
    else:
        raise JobOpsError("没有可保存的提示词")
    return _after_save(settings, job, directory)


def preview_clip_script(
    settings: Settings,
    job_id: str,
    clip_id: str,
    *,
    script_zh: str | None = None,
    requirement: str | None = None,
    h3_seconds: float | None = None,
) -> dict[str, Any]:
    """只生成预览，不改盘。返回中文稿、英文 H3 和结构化 JSON。"""
    _reject_if_busy(settings)
    directory, job = get_job(settings, job_id)
    clips_doc = _load_json(directory / "clips.json") or {}
    clips = list(clips_doc.get("clips") or [])
    clip = next((c for c in clips if str(c.get("id")) == clip_id), None)
    if clip is None:
        raise JobOpsError(f"没有这一段：{clip_id}")
    path = normalize_generate_path(
        clips_doc.get("generate_path") or (job.get("options") or {}).get("generate_path") or "t2va_turbo"
    )
    if h3_seconds is not None:
        frames, seconds = snap_seconds(float(h3_seconds), settings)
        clip = dict(clip)
        clip["h3_frames"] = frames
        clip["h3_seconds"] = seconds
        src = float(clip.get("source_seconds") or 0)
        clip["drift"] = round(seconds - src, 3)
        clip["padded"] = seconds > src + 0.05
    current = _load_json(directory / "prompts" / f"{clip_id}.json") or {}
    if not isinstance(current, dict):
        raise JobOpsError(f"{clip_id}: prompts/{clip_id}.json 不是 JSON 对象")
    facts = _clip_facts_for(directory, clip, clips)
    edit: dict[str, Any] = {"mode": "manual" if script_zh is not None else "ai"}
    if script_zh is not None:
        edit["script_zh"] = script_zh
    else:
        edit["requirement"] = requirement or ""
    try:
        doc, txt = rewrite_clip(
            settings,
            clip,
            facts,
            path=path,
            current=current,
            edit=edit,
            log=lambda text: None,
        )
    except Exception as exc:  # noqa: BLE001
        raise JobOpsError(f"AI 未生成有效英文脚本，请调整中文说明后重试：{exc}") from exc
    return {
        "clip_id": clip_id,
        "h3_seconds": clip["h3_seconds"],
        "script_zh": assemble_zh(doc, clip),
        "prompt_txt": txt,
        "review_md": assemble_md(doc, clip, facts, txt),
        "prompt_json": doc,
    }


def _batch_path(directory: Path) -> Path:
    return directory / "rewrite_batch.json"


def reap_stale_rewrite_batches(settings: Settings) -> None:
    """serve 启动时清理：上一进程留下的 running 批量改写线程已死，不可能自愈。
    已有完成预览的转 ready（供保存），全 pending 的直接删。"""
    for job in iter_jobs(settings):
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        directory = job_dir(settings, job_id)
        batch = _load_batch(directory)
        if batch is None or batch.get("state") != "running":
            continue
        recs = [rec for rec in (batch.get("clips") or {}).values() if isinstance(rec, dict)]
        done = [rec for rec in recs if rec.get("state") == "done"]
        if done:
            batch["state"] = "ready"
            atomic_write_json(_batch_path(directory), batch)
        else:
            _batch_path(directory).unlink(missing_ok=True)


def _load_batch(directory: Path) -> dict[str, Any] | None:
    batch = _load_json(_batch_path(directory))
    return batch if isinstance(batch, dict) else None


def start_rewrite_batch(
    settings: Settings,
    job_id: str,
    clip_ids: list[str],
    requirement: str,
) -> dict[str, Any]:
    """批量 AI 改写：后台逐段生成预览（不落盘），结果写 rewrite_batch.json 供勾选保存。"""
    _reject_if_busy(settings)
    ids = [str(c).strip() for c in clip_ids if str(c).strip()]
    if not ids:
        raise JobOpsError("没有选择片段")
    if not str(requirement or "").strip():
        raise JobOpsError("请填写修改需求")
    directory, job = get_job(settings, job_id)
    clips_doc = _load_json(directory / "clips.json") or {}
    clips = list(clips_doc.get("clips") or [])
    known = {str(c.get("id")) for c in clips}
    unknown = [cid for cid in ids if cid not in known]
    if unknown:
        raise JobOpsError(f"没有这些片段：{', '.join(unknown)}")
    existing = _load_batch(directory)
    if existing is not None and existing.get("state") == "running":
        raise JobOpsError("上一轮批量改写还在跑，等它结束")
    if existing is not None and existing.get("state") == "ready":
        raise JobOpsError("上一轮批量改写结果还没处理：先保存或放弃，再发起新一轮")
    from datetime import datetime, timezone

    batch: dict[str, Any] = {
        "requirement": str(requirement).strip(),
        "clip_ids": ids,
        "state": "running",
        "clips": {cid: {"state": "pending", "error": None, "preview": None} for cid in ids},
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    atomic_write_json(_batch_path(directory), batch)

    from vrs.worker import spawn

    def work() -> None:
        with JobLock(settings).hold(job_id):
            _run_rewrite_batch(settings, job_id, directory, clips_doc, batch)

    spawn(settings, job_id, work)
    return batch


def _run_rewrite_batch(
    settings: Settings,
    job_id: str,
    directory: Path,
    clips_doc: dict[str, Any],
    batch: dict[str, Any],
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime, timezone

    batch_lock = threading.Lock()

    def touch() -> None:
        batch["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        atomic_write_json(_batch_path(directory), batch)

    clips = list(clips_doc.get("clips") or [])
    path = normalize_generate_path(
        clips_doc.get("generate_path") or "t2va_turbo"
    )
    cfg = settings.providers.get("llm") or {}
    concurrency = max(1, int(cfg.get("sdk_concurrency") or 3))

    def rewrite_one(cid: str) -> None:
        rec = (batch.get("clips") or {}).get(cid) or {"state": "pending", "error": None, "preview": None}
        try:
            clip = next((c for c in clips if str(c.get("id")) == cid), None)
            if clip is None:
                raise JobOpsError(f"没有这一段：{cid}")
            current = _load_json(directory / "prompts" / f"{cid}.json")
            if not isinstance(current, dict):
                current = {}
            facts = _clip_facts_for(directory, clip, clips)
            doc_new, txt = rewrite_clip(
                settings,
                clip,
                facts,
                path=path,
                current=current,
                edit={"mode": "ai", "requirement": str(batch.get("requirement") or "")},
                log=lambda text: None,
            )
            rec["preview"] = {
                "script_zh": assemble_zh(doc_new, clip),
                "prompt_txt": txt,
                "review_md": assemble_md(doc_new, clip, facts, txt),
                "prompt_json": doc_new,
            }
            rec["state"] = "done"
            rec["error"] = None
        except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其余段
            rec["state"] = "failed"
            rec["error"] = str(exc).split("\n")[0][:300] or type(exc).__name__
        with batch_lock:
            batch["clips"][cid] = rec
            touch()

    ids = [cid for cid in (batch.get("clip_ids") or []) if cid]
    if concurrency > 1 and len(ids) > 1:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(ids))) as pool:
            futures = [pool.submit(rewrite_one, cid) for cid in ids]
            for future in as_completed(futures):
                future.result()
    else:
        for cid in ids:
            rewrite_one(cid)
    batch["state"] = "ready"
    touch()


def rewrite_batch_status(settings: Settings, job_id: str, *, full: bool = False) -> dict[str, Any]:
    directory, _job = get_job(settings, job_id)
    batch = _load_batch(directory)
    if batch is None:
        return {"state": "none"}
    if not full:
        slim = dict(batch)
        slim.pop("clips", None)
        slim["clips"] = {
            cid: {"state": rec.get("state"), "error": rec.get("error")}
            for cid, rec in (batch.get("clips") or {}).items()
            if isinstance(rec, dict)
        }
        return slim
    return batch


def discard_rewrite_batch(settings: Settings, job_id: str) -> dict[str, Any]:
    directory, _job = get_job(settings, job_id)
    batch = _load_batch(directory)
    if batch is not None and batch.get("state") == "running":
        raise JobOpsError("批量改写还在跑，等它结束")
    _batch_path(directory).unlink(missing_ok=True)
    return {"state": "none"}


def save_rewrite_batch(
    settings: Settings,
    job_id: str,
    clip_ids: list[str],
) -> dict[str, Any]:
    """把勾选段的批量改写预览正式写入 prompts/，未选段丢弃，随后清理 batch 文件。"""
    directory, job = get_job(settings, job_id)
    batch = _load_batch(directory)
    if batch is None:
        raise JobOpsError("没有待保存的批量改写")
    if batch.get("state") != "ready":
        raise JobOpsError("批量改写还没完成")
    clips_doc = _load_json(directory / "clips.json") or {}
    clips = list(clips_doc.get("clips") or [])
    path = normalize_generate_path(
        clips_doc.get("generate_path") or (job.get("options") or {}).get("generate_path") or "t2va_turbo"
    )
    saved: list[str] = []
    skipped: list[str] = []
    for cid in clip_ids:
        rec = (batch.get("clips") or {}).get(cid) or {}
        preview = rec.get("preview")
        clip = next((c for c in clips if str(c.get("id")) == cid), None)
        if rec.get("state") != "done" or not isinstance(preview, dict) or clip is None:
            skipped.append(cid)
            continue
        facts = _clip_facts_for(directory, clip, clips)
        _write_prompt_files(directory, clip, preview.get("prompt_json") or {}, path, facts=facts)
        saved.append(cid)
    _batch_path(directory).unlink(missing_ok=True)
    if not saved:
        raise JobOpsError("没有可保存的片段（所选段改写失败或不存在）")
    result = _after_save(settings, job, directory)
    if isinstance(result, dict):
        result["saved"] = saved
        result["skipped"] = skipped
    return result


def save_all_json(
    settings: Settings,
    job_id: str,
    *,
    clips_doc: dict[str, Any] | None = None,
    by_clip: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _reject_if_busy(settings)
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
    from vrs.f2douyin import cookie_expired, cookie_ready

    smtp = settings.smtp or {}
    comfy = settings.providers.get("comfy") or {}
    vl = settings.providers.get("vl") or {}
    llm = settings.providers.get("llm") or {}
    cookie_ok, cookie_detail = cookie_ready(settings)
    expired = bool(cookie_ok and cookie_expired(settings))
    if not cookie_ok:
        state = "missing"
        status_label = "未配置"
    elif expired:
        state = "expired"
        status_label = "登录已失效，需要重新导入"
    else:
        state = "ok"
        status_label = cookie_detail
    from_env = bool((os.environ.get("VRS_DOUYIN_COOKIE") or "").strip())
    return {
        "mode": settings.mode(),
        "mock_speed": settings.mock_speed(),
        "mock_faults": settings.default.get("mock_faults") or {},
        "bind_host": settings.bind_host(),
        "bind_port": settings.bind_port(),
        "comfy_base_url": str(comfy.get("base_url") or "http://127.0.0.1:8188"),
        "llm_kind": str(llm.get("kind") or "anthropic_sdk"),
        "llm_base_url": str(llm.get("base_url") or ""),
        "llm_model": str(llm.get("model") or ""),
        "llm_has_api_key": bool(str(llm.get("api_key") or "").strip() or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        "llm_api_key_from_env": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        "vl_kind": str(vl.get("kind") or llm.get("kind") or "anthropic_sdk"),
        "vl_model": str(vl.get("model") or llm.get("model") or ""),
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
        "smtp_user": str(smtp.get("user") or ""),
        "smtp_has_password": bool(str(smtp.get("password") or "").strip()),
        "smtp_password_from_env": bool((os.environ.get("VRS_SMTP_PASSWORD") or "").strip()),
        "smtp_hint": "授权码只保存在本机 config/smtp.local.yaml，页面不回显。QQ 邮箱请用授权码，不是登录密码。开跑只检查是否已填邮箱和授权码，不登录服务器。通不通请用「发送测试」。",
        "douyin_cookie_set": cookie_ok,
        "douyin_cookie_from_env": from_env,
        "douyin_cookie_expired": expired,
        "douyin_cookie_state": state,
        "douyin_cookie_status": status_label,
        "douyin_cookie_hint": (
            "网页读不了 douyin.com 标签页里的登录态。"
            "未配置或登录失效时可以一键导入或粘贴；已配置时点「更换」。"
            + (
                "当前实际使用环境变量 VRS_DOUYIN_COOKIE，写入本机配置不会覆盖它，过期时请先清掉环境变量。"
                if from_env
                else ""
            )
        ),
        "t2va": t2va_defaults(settings),
        "t2va_workflows": [
            {"id": "video_minimax_h3_t2v_turbo.json", "label": "T2VA Turbo"},
            {"id": "video_minimax_h3_t2v.json", "label": "T2VA 非 LoRA"},
        ],
    }


def _smtp_local_doc(settings: Settings) -> dict[str, Any]:
    path = settings.root / "config" / "smtp.local.yaml"
    if path.is_file():
        loaded = load_yaml(path)
        if isinstance(loaded, dict) and loaded:
            return dict(loaded)
    cfg = settings.smtp or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "host": str(cfg.get("host") or "smtp.qq.com"),
        "port": int(cfg.get("port") or 465),
        "user": str(cfg.get("user") or ""),
        "password": str(cfg.get("password") or ""),
        "from_addr": str(cfg.get("from_addr") or ""),
        "to": list(cfg.get("to") or []),
        "security": str(cfg.get("security") or "ssl"),
        "max_attachment_mb": cfg.get("max_attachment_mb") or 20,
    }


def _save_smtp_local(settings: Settings, doc: dict[str, Any]) -> None:
    path = settings.root / "config" / "smtp.local.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def patch_settings(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    forbidden = {"password", "api_key", "cookie"}
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
    llm_patch_keys = ("llm_kind", "llm_base_url", "llm_model", "llm_api_key")
    if any(body.get(key) is not None for key in llm_patch_keys):
        providers = dict(current.get("providers") or {})
        llm = dict(providers.get("llm") or {})
        if body.get("llm_kind") is not None:
            kind = str(body["llm_kind"]).strip()
            if kind not in {"anthropic_sdk", "cursor_sdk"}:
                raise JobOpsError("Provider 只能是 anthropic_sdk 或 cursor_sdk")
            llm["kind"] = kind
        if body.get("llm_base_url") is not None:
            llm["base_url"] = str(body["llm_base_url"]).strip().rstrip("/")
        if body.get("llm_model") is not None:
            llm["model"] = str(body["llm_model"]).strip()
        if body.get("llm_api_key") is not None:
            # 空串 = 清除，回落到环境变量 ANTHROPIC_API_KEY
            llm["api_key"] = str(body["llm_api_key"]).strip()
        if llm:
            providers["llm"] = llm
        current["providers"] = providers
    if body.get("gpu_memory_gb") is not None:
        h3 = dict(current.get("h3") or {})
        h3["gpu_memory_gb"] = float(body["gpu_memory_gb"])
        current["h3"] = h3
    if body.get("t2va") is not None:
        try:
            snapshot = merge_t2va_snapshot(settings, body.get("t2va") if isinstance(body.get("t2va"), dict) else None)
        except ValueError as exc:
            raise JobOpsError(str(exc)) from exc
        h3 = dict(current.get("h3") or {})
        h3["t2va"] = snapshot
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
    smtp_secret_keys = {"smtp_password", "smtp_user"}
    if any(key in body and body[key] is not None for key in smtp_secret_keys):
        smtp_doc = _smtp_local_doc(settings)
        if body.get("smtp_enabled") is not None:
            smtp_doc["enabled"] = bool(body["smtp_enabled"])
        if smtp.get("to"):
            smtp_doc["to"] = list(smtp["to"])
        if "smtp_user" in body and body["smtp_user"] is not None:
            user = str(body["smtp_user"]).strip()
            smtp_doc["user"] = user
            if user:
                smtp_doc["from_addr"] = user
        if "smtp_password" in body and body["smtp_password"] is not None:
            smtp_doc["password"] = str(body["smtp_password"]).strip()
        if not str(smtp_doc.get("user") or "").strip():
            tos = [str(item).strip() for item in (smtp_doc.get("to") or []) if str(item).strip()]
            if tos:
                smtp_doc["user"] = tos[0]
                smtp_doc["from_addr"] = str(smtp_doc.get("from_addr") or tos[0])
        if not str(smtp_doc.get("host") or "").strip() or str(smtp_doc.get("host")) == "smtp.example.com":
            smtp_doc["host"] = "smtp.qq.com"
            smtp_doc["port"] = 465
            smtp_doc["security"] = "ssl"
        _save_smtp_local(settings, smtp_doc)
    if "douyin_cookie" in body and body["douyin_cookie"] is not None:
        cookie = str(body["douyin_cookie"]).strip()
        if cookie and any(ord(ch) > 127 for ch in cookie):
            raise JobOpsError("抖音 Cookie 含非 ASCII 字符，请从 Chrome 重新复制")
        douyin = dict(current.get("douyin") or {})
        douyin["cookie"] = cookie
        current["douyin"] = douyin
        runtime["douyin_cookie_status"] = "ok"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(
        yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    settings.reload()
    return settings_public(settings)


def import_douyin_cookie(settings: Settings, *, force_window: bool = False) -> dict[str, Any]:
    from vrs.douyincookie import DouyinCookieError, grab_douyin_cookie

    try:
        cookie, source = grab_douyin_cookie(settings, force_window=force_window)
    except DouyinCookieError as exc:
        raise JobOpsError(str(exc)) from exc
    public = patch_settings(settings, {"douyin_cookie": cookie})
    public["douyin_cookie_imported_via"] = source
    return public
