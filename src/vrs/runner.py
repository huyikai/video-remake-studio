from __future__ import annotations

import json
import shutil
from pathlib import Path

from vrs.aspect import aspect_mismatch
from vrs.cancel import JobCancelled, clear_cancel, raise_if_cancelled, request_cancel
from vrs.envcheck import collect_env
from vrs.jobstore import create_job, get_job, iter_jobs, mark_stage, save_status
from vrs.lock import BusyError, JobLock
from vrs.mailer import failure_mail_body, failure_mail_subject, send_mail
from vrs.settings import Settings
from vrs.smtpcheck import smtp_credentials_ready, smtp_enabled
from vrs.stages.download import run_download
from vrs.stages.finish import run_finish
from vrs.stages.generate import clip_output_dir, job_generate_path, quality_complete, run_generate
from vrs.stages.pagemeta import run_pagemeta
from vrs.stages.precheck import run_precheck
from vrs.stages.script import run_script
from vrs.stages.understand import run_understand


class IngestGateError(RuntimeError):
    pass


def _gate(settings: Settings, *, kind: str, want_smtp: bool, url: str | None = None) -> None:
    env = collect_env(settings, stage="download")
    reasons: list[str] = []
    if not env["gate_new_job"]["ok"]:
        reasons.extend(env["gate_new_job"]["reasons"])
    if kind == "url":
        from vrs.f2douyin import cookie_ready, is_douyin_url

        if url and is_douyin_url(url):
            f2 = next((i for i in env["install"] if i["id"] == "f2"), None)
            if f2 and not f2["ok"]:
                reasons.append("抖音进料需要安装 f2（uv sync）")
            cookie_ok, cookie_detail = cookie_ready(settings)
            if not cookie_ok:
                reasons.append(cookie_detail)
        else:
            ytdlp = next((i for i in env["install"] if i["id"] == "yt-dlp"), None)
            if ytdlp and not ytdlp["ok"]:
                reasons.append("URL 进料需要 yt-dlp")
    ffmpeg = next((i for i in env["install"] if i["id"] == "ffmpeg"), None)
    if ffmpeg and not ffmpeg["ok"]:
        reasons.append("缺少 ffmpeg")
    if want_smtp and smtp_enabled(settings.smtp) and not smtp_credentials_ready(settings.smtp):
        reasons.append("SMTP 已启用但未配置邮箱或授权码")
    uniq: list[str] = []
    for item in reasons:
        if item not in uniq:
            uniq.append(item)
    if uniq:
        raise IngestGateError("；".join(uniq))


def _clips(directory: Path) -> list[dict]:
    path = directory / "clips.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return list((data or {}).get("clips") or [])


def mark_job_cancelled(settings: Settings, job_id: str) -> dict:
    directory, job = get_job(settings, job_id)
    job["state"] = "cancelled"
    job["note"] = "用户放弃"
    stage = str(job.get("stage") or "download")
    rec = (job.get("stages") or {}).setdefault(stage, {})
    if rec.get("status") == "running":
        rec["status"] = "pending"
        rec["error"] = "用户放弃"
    save_status(job, directory)
    return job


def _pause_for_aspect(settings: Settings, job: dict, directory: Path) -> dict:
    options = job.setdefault("options", {})
    if options.get("aspect_confirmed"):
        job["need_aspect_confirm"] = False
        save_status(job, directory)
        return job
    default = str(options.get("aspect_ratio") or settings.default.get("aspect_ratio") or "16:9")
    if not settings.default.get("aspect_confirm", True):
        options.setdefault("aspect_ratio", default)
        options["aspect_confirmed"] = True
        job["need_aspect_confirm"] = False
        save_status(job, directory)
        return job
    probe = (job.get("source") or {}).get("probe") or {}
    label, mismatch = aspect_mismatch(probe.get("width"), probe.get("height"), default)
    if label:
        job["source_aspect"] = label
    if not mismatch:
        options["aspect_confirmed"] = True
        job["need_aspect_confirm"] = False
        save_status(job, directory)
        return job
    job["need_aspect_confirm"] = True
    job["state"] = "paused"
    job["note"] = (
        f"原片是 {label}，默认输出 {default}。"
        "确认跟原片比例还是保持 16:9 重构后再继续。"
    )
    save_status(job, directory)
    return job


def _continue_generate(settings: Settings, job: dict, directory: Path) -> dict:
    raise_if_cancelled(directory)
    if job.get("stages", {}).get("precheck", {}).get("status") != "done":
        return job
    if job.get("stages", {}).get("finish", {}).get("status") == "done":
        return job
    clips = _clips(directory)
    mode = str((job.get("options") or {}).get("review_mode") or "pause_draft")
    path = job_generate_path(directory, job)
    if clips and not quality_complete(directory, clips, "draft", path=path):
        job = run_generate(settings, job, directory, quality="draft")
        if job.get("stages", {}).get("generate", {}).get("status") != "done":
            return job
        if mode != "full_auto":
            return job
    if mode == "full_auto":
        from vrs.stages.draftreview import run_auto_review

        job = run_auto_review(settings, job, directory)
        if job.get("state") in {"failed", "cancelled"}:
            return job
        gen_status = str((job.get("stages") or {}).get("generate", {}).get("status") or "")
        if gen_status == "waiting":
            return job
        if clips and not quality_complete(directory, clips, "final", path=path):
            job = run_generate(settings, job, directory, quality="final")
            if job.get("stages", {}).get("generate", {}).get("status") != "done":
                return job
        raise_if_cancelled(directory)
        return run_finish(settings, job, directory)
    if clips and not quality_complete(directory, clips, "final", path=path):
        return run_generate(settings, job, directory, quality="final")
    raise_if_cancelled(directory)
    return run_finish(settings, job, directory)


def _continue_understand(settings: Settings, job: dict, directory: Path) -> dict:
    raise_if_cancelled(directory)
    pagemeta = job.get("stages", {}).get("pagemeta", {}).get("status")
    if pagemeta not in {"done", "skipped"}:
        return job
    if job.get("stages", {}).get("understand", {}).get("status") != "done":
        job = run_understand(settings, job, directory)
    raise_if_cancelled(directory)
    if job.get("stages", {}).get("understand", {}).get("status") != "done":
        return job
    if job.get("stages", {}).get("script", {}).get("status") != "done":
        job = run_script(settings, job, directory)
    raise_if_cancelled(directory)
    if job.get("stages", {}).get("script", {}).get("status") != "done":
        return job
    if job.get("stages", {}).get("precheck", {}).get("status") != "done":
        job = run_precheck(settings, job, directory)
    return job


def _run_created(settings: Settings, job_id: str) -> dict:
    directory, job = get_job(settings, job_id)
    clear_cancel(directory)
    kind = job["source"]["kind"]
    lock = JobLock(settings)
    try:
        with lock.hold(job_id):
            raise_if_cancelled(directory)
            job = run_download(settings, job, directory)
            if job.get("stages", {}).get("download", {}).get("status") != "done":
                return job
            send_mail(
                settings.smtp,
                subject=f"VRS 开始 {job['id']}",
                body=(
                    f"job {job['id']}\nkind {kind}\n"
                    f"path {job.get('options', {}).get('generate_path')}\n"
                    f"review {job.get('options', {}).get('review_mode')}"
                ),
            )
            job = _pause_for_aspect(settings, job, directory)
            if job.get("need_aspect_confirm"):
                return job
            raise_if_cancelled(directory)
            job = run_pagemeta(settings, job, directory)
            job = _continue_understand(settings, job, directory)
            mode = str((job.get("options") or {}).get("review_mode") or "pause_draft")
            if (
                mode == "full_auto"
                and job.get("stages", {}).get("precheck", {}).get("status") == "done"
            ):
                return _continue_generate(settings, job, directory)
            return job
    except JobCancelled:
        return mark_job_cancelled(settings, job_id)
    except BusyError:
        if job.get("stages", {}).get("download", {}).get("status") == "pending":
            shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as exc:
        _, failed = get_job(settings, job_id)
        err = str(exc).split("\nTraceback")[0].strip() or "异常退出"
        if failed.get("state") == "running":
            failed["state"] = "failed"
        if not str(failed.get("note") or "").strip() or str(failed.get("note")).lower() in {"failed", "running"}:
            failed["note"] = err
            save_status(failed, directory)
        send_mail(
            settings.smtp,
            subject=failure_mail_subject(failed, exc=exc),
            body=failure_mail_body(failed, exc=exc, directory=directory),
        )
        return failed


def create_and_download(
    settings: Settings,
    *,
    url: str | None = None,
    file_path: str | None = None,
    review_mode: str | None = None,
    generate_path: str = "t2va",
    smtp: bool | None = None,
    vl_mode: str | None = None,
    aspect_ratio: str | None = None,
    aspect_confirmed: bool = False,
    background: bool = False,
    generate: dict | None = None,
) -> dict:
    if bool(url) == bool(file_path):
        raise IngestGateError("必须只提供 url 或本地绝对路径其中一个")
    kind = "url" if url else "file"
    original = str(Path(file_path).expanduser()) if file_path else None
    if kind == "file":
        path = Path(original)
        if not path.is_absolute():
            raise IngestGateError("本地进料必须是绝对路径")
        if settings.mode() != "mock" and not path.is_file():
            raise IngestGateError(f"找不到文件：{path}")
    want_smtp = settings.smtp.get("enabled") if smtp is None else smtp
    if settings.mode() != "mock":
        _gate(settings, kind=kind, want_smtp=bool(want_smtp), url=url)
    if background and JobLock(settings).occupied():
        raise BusyError("已有任务在跑，等它结束或先放弃")
    job = create_job(
        settings,
        kind=kind,
        url=url,
        original_path=original,
        review_mode=review_mode,
        generate_path=generate_path,
        smtp=smtp,
        vl_mode=vl_mode,
        aspect_ratio=aspect_ratio,
        aspect_confirmed=aspect_confirmed,
        generate=generate,
    )
    if settings.mode() == "mock":
        from vrs.mock import start_created

        start_created(settings, job["id"])
        return job
    if background:
        from vrs.worker import spawn

        spawn(settings, job["id"], lambda: _run_created(settings, job["id"]))
        return job
    return _run_created(settings, job["id"])


def resume_download(
    settings: Settings,
    job_id: str,
    *,
    only_clips_arg: list[str] | None = None,
    force_quality: str | None = None,
) -> dict:
    directory, job = get_job(settings, job_id)
    clear_cancel(directory)
    if only_clips_arg:
        job.setdefault("options", {})["only_clips"] = only_clips_arg
        save_status(job, directory)
    if job.get("need_aspect_confirm") and not (job.get("options") or {}).get("aspect_confirmed"):
        return job
    kind = job["source"]["kind"]
    if settings.mode() == "mock":
        from vrs.mock import resume_now as mock_resume

        return mock_resume(settings, job_id)
    try:
        _gate(
            settings,
            kind=kind,
            want_smtp=bool(job.get("options", {}).get("smtp")),
            url=job.get("source", {}).get("url"),
        )
    except IngestGateError as exc:
        stage = str(job.get("stage") or "download")
        mark_stage(job, directory, stage, "failed", error=str(exc))
        job["note"] = str(exc)
        save_status(job, directory)
        return job
    lock = JobLock(settings)
    with lock.hold(job_id):
        try:
            raise_if_cancelled(directory)
            if job["stages"]["download"]["status"] != "done":
                job = run_download(settings, job, directory)
            if job.get("stages", {}).get("download", {}).get("status") != "done":
                return job
            job = _pause_for_aspect(settings, job, directory)
            if job.get("need_aspect_confirm"):
                return job
            pagemeta = job.get("stages", {}).get("pagemeta", {}).get("status")
            if pagemeta not in {"done", "skipped"}:
                job = run_pagemeta(settings, job, directory)
            job = _continue_understand(settings, job, directory)
            if job.get("stages", {}).get("precheck", {}).get("status") != "done":
                return job
            if force_quality == "draft":
                return run_generate(settings, job, directory, quality="draft")
            if force_quality == "final":
                return run_generate(settings, job, directory, quality="final")
            if force_quality == "assemble":
                return run_finish(settings, job, directory)
            return _continue_generate(settings, job, directory)
        except JobCancelled:
            return mark_job_cancelled(settings, job_id)
        except Exception as exc:
            _, failed = get_job(settings, job_id)
            err = str(exc).split("\nTraceback")[0].strip() or "续跑中途异常退出"
            if failed.get("state") == "running":
                failed["state"] = "failed"
                failed["note"] = failed.get("note") or err
                save_status(failed, directory)
            elif not str(failed.get("note") or "").strip() or str(failed.get("note")).lower() in {"failed", "running"}:
                failed["note"] = err
                save_status(failed, directory)
            send_mail(
                settings.smtp,
                subject=failure_mail_subject(failed, exc=exc),
                body=failure_mail_body(failed, exc=exc, directory=directory),
            )
            return failed


def rerun_drafts(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> dict:
    if settings.mode() == "mock":
        from vrs.mock import draft_now as mock_draft

        return mock_draft(settings, job_id, clip_ids)
    directory, job = get_job(settings, job_id)
    if job.get("stages", {}).get("precheck", {}).get("status") != "done":
        raise IngestGateError("预检还没过，不能出试片")
    path = job_generate_path(directory, job)
    clips = _clips(directory)
    wanted = [c for c in (clip_ids or []) if c]
    if not wanted:
        wanted = [str(c["id"]) for c in clips]
    for clip_id in wanted:
        draft_dest = clip_output_dir(directory, path, "draft") / f"{clip_id}.mp4"
        draft_dest.unlink(missing_ok=True)
        # 脚本变了，成片也必须作废，否则会一直卡在「再出试片」且成片永不重生成
        final_dest = clip_output_dir(directory, path, "final") / f"{clip_id}.mp4"
        final_dest.unlink(missing_ok=True)
    concat = directory / "output" / path / "draft.mp4"
    concat.unlink(missing_ok=True)
    final_concat = directory / "output" / path / "final.mp4"
    final_concat.unlink(missing_ok=True)
    gen = job.get("stages", {}).get("generate") or {}
    if gen.get("status") == "done":
        gen["status"] = "pending"
        gen["error"] = None
        save_status(job, directory)
    if str(job.get("state") or "").lower() == "done":
        job["state"] = "paused"
        job["note"] = "脚本已改，正在重新出试片"
        save_status(job, directory)
    return resume_download(settings, job_id, force_quality="draft")


def run_finals(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> dict:
    if settings.mode() == "mock":
        from vrs.mock import final_now as mock_final

        return mock_final(settings, job_id, clip_ids)
    directory, job = get_job(settings, job_id)
    clips = _clips(directory)
    if not clips:
        raise IngestGateError("还没有片段，不能出成片")
    wanted = [item for item in (clip_ids or []) if item]
    path = job_generate_path(directory, job)
    if wanted:
        for clip_id in wanted:
            (clip_output_dir(directory, path, "final") / f"{clip_id}.mp4").unlink(missing_ok=True)
        (directory / "output" / path / "final.mp4").unlink(missing_ok=True)
        if str(job.get("state") or "").lower() == "done":
            job["state"] = "paused"
            job["note"] = "正在更新各段成片"
            save_status(job, directory)
    elif not quality_complete(directory, clips, "draft"):
        raise IngestGateError("试片还没齐，不能出成片")
    return resume_download(settings, job_id, force_quality="final")


def run_assemble(settings: Settings, job_id: str) -> dict:
    if settings.mode() == "mock":
        from vrs.mock import assemble_now as mock_assemble

        return mock_assemble(settings, job_id)
    directory, job = get_job(settings, job_id)
    clips = _clips(directory)
    if not clips or not quality_complete(directory, clips, "final"):
        raise IngestGateError("各段成片还没齐，不能拼接")
    return resume_download(settings, job_id, force_quality="assemble")


def cancel_job(settings: Settings, job_id: str) -> dict:
    directory, job = get_job(settings, job_id)
    request_cancel(directory)
    try:
        from vrs.comfyclient import interrupt

        interrupt(settings)
    except Exception:
        pass
    occ = JobLock(settings).occupied()
    if not occ or occ.get("job_id") != job_id:
        return mark_job_cancelled(settings, job_id)
    directory, job = get_job(settings, job_id)
    job["note"] = "正在放弃…"
    progress = job.get("understand_progress")
    if isinstance(progress, dict):
        progress = dict(progress)
        progress["chip"] = "正在放弃"
        job["understand_progress"] = progress
    save_status(job, directory)
    return job


def list_jobs(settings: Settings) -> list[dict]:
    return iter_jobs(settings)
