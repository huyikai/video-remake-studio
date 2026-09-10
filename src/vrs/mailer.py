"""可选 SMTP。失败只记日志，不改变 Job 状态。"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from vrs.smtpcheck import format_smtp_error, smtp_enabled

_STAGE_LABEL = {
    "download": "下载",
    "pagemeta": "页面信息",
    "understand": "视频理解",
    "script": "脚本生成",
    "precheck": "预检",
    "generate": "视频生成",
    "finish": "成片",
}
_STATE_LABEL = {
    "running": "运行中",
    "paused": "已暂停",
    "failed": "失败",
    "error": "失败",
    "cancelled": "已取消",
    "canceled": "已取消",
    "done": "完成",
    "pending": "等待",
    "waiting": "等待",
}


def _log(log: Callable[[str], None] | None, text: str) -> None:
    if log:
        log(text)


def _plain(text: Any, *, limit: int = 800) -> str:
    raw = str(text or "").split("\nTraceback")[0].strip()
    if len(raw) > limit:
        return raw[: limit - 1] + "…"
    return raw


def _stage_label(name: str) -> str:
    return _STAGE_LABEL.get(name, name or "未知")


def failure_mail_subject(job: dict[str, Any], *, exc: BaseException | None = None) -> str:
    job_id = str(job.get("id") or "")
    failed_name = ""
    for name, rec in (job.get("stages") or {}).items():
        if isinstance(rec, dict) and str(rec.get("status") or "").lower() == "failed":
            failed_name = name
            break
    stage = failed_name or str(job.get("stage") or "")
    label = _stage_label(stage) if stage else ""
    if label:
        return f"VRS 失败 {job_id} · {label}"
    return f"VRS 失败 {job_id}"


def failure_mail_body(
    job: dict[str, Any],
    *,
    exc: BaseException | None = None,
    directory: Path | None = None,
) -> str:
    job_id = str(job.get("id") or "")
    state = str(job.get("state") or "")
    stage = str(job.get("stage") or "")
    note = _plain(job.get("note"))
    progress = job.get("understand_progress") if isinstance(job.get("understand_progress"), dict) else {}
    chip = _plain(progress.get("chip"), limit=80)
    detail = _plain(progress.get("detail"), limit=400)
    source = job.get("source") if isinstance(job.get("source"), dict) else {}
    source_text = _plain(source.get("url") or source.get("original_path") or "", limit=300)
    lines = [
        f"任务：{job_id}",
        f"状态：{_STATE_LABEL.get(state.lower(), state or '未知')}",
        f"阶段：{_stage_label(stage)}" if stage else "阶段：未知",
    ]
    if chip:
        lines.append(f"当前步骤：{chip}")
    if source_text:
        lines.append(f"输入：{source_text}")
    if directory is not None:
        lines.append(f"本机目录：{directory.resolve()}")

    reasons: list[str] = []
    if note and note.lower() not in {"failed", "running", "error"}:
        reasons.append(note)
    if detail and detail not in reasons:
        reasons.append(detail)
    if exc is not None:
        err = _plain(exc)
        if err and err not in reasons:
            reasons.append(err)
    for name, rec in (job.get("stages") or {}).items():
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("status") or "").lower()
        error = _plain(rec.get("error"))
        if error and status in {"failed", "waiting", "error"}:
            item = f"{_stage_label(name)}（{status}）：{error}"
            if item not in reasons and error not in reasons:
                reasons.append(item)
    lines.append("")
    lines.append("原因：")
    if reasons:
        lines.extend(f"- {item}" for item in reasons)
    else:
        lines.append("- 没有留下更具体的错误，请打开任务详情或 logs/worker.log")
    return "\n".join(lines)


def _smtp_login(client: smtplib.SMTP, user: str, password: str) -> None:
    client.user = user
    client.password = password
    client.ehlo_or_helo_if_needed()
    mechanisms = (client.esmtp_features.get("auth") or "").upper().split()
    if "LOGIN" in mechanisms:
        client.auth("LOGIN", client.auth_login)
        return
    client.login(user, password)


def send_mail(
    cfg: dict[str, Any],
    *,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    log: Callable[[str], None] | None = None,
) -> str | None:
    if not smtp_enabled(cfg):
        return "SMTP 未启用"
    host = str(cfg.get("host") or "")
    port = int(cfg.get("port") or 0)
    user = str(cfg.get("user") or "")
    password = str(cfg.get("password") or "")
    from_addr = str(cfg.get("from_addr") or user)
    to_raw = cfg.get("to") or []
    if isinstance(to_raw, str):
        to_list = [p.strip() for p in to_raw.split(",") if p.strip()]
    else:
        to_list = [str(x).strip() for x in to_raw if str(x).strip()]
    if not host or not port or not from_addr or not to_list:
        msg = "SMTP 已启用但缺少 host/from/to，跳过"
        _log(log, msg)
        return msg
    if not user or not password:
        msg = "SMTP 已启用但未配置邮箱或授权码，跳过"
        _log(log, msg)
        return msg
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)
    cap = float(cfg.get("max_attachment_mb") or 20) * 1024 * 1024
    used = 0.0
    for path in attachments or []:
        if not path.is_file():
            continue
        size = path.stat().st_size
        if used + size > cap:
            msg.set_content(
                body
                + f"\n\n未附 {path}（超过 {cfg.get('max_attachment_mb') or 20} MB）。本机路径：{path.resolve()}"
            )
            continue
        used += size
        msg.add_attachment(
            path.read_bytes(),
            maintype="video" if path.suffix.lower() == ".mp4" else "application",
            subtype="mp4" if path.suffix.lower() == ".mp4" else "octet-stream",
            filename=path.name,
        )
    security = str(cfg.get("security") or "starttls").lower()
    client: smtplib.SMTP | None = None
    try:
        if security == "ssl":
            context = ssl.create_default_context()
            client = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=20)
            if security == "starttls":
                client.starttls(context=ssl.create_default_context())
        _smtp_login(client, user, password)
        client.send_message(msg)
        _log(log, f"已发信：{subject}")
        return None
    except Exception as exc:  # noqa: BLE001
        detail = format_smtp_error(exc)
        _log(log, f"发信失败（任务状态不变）：{detail}")
        return detail
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
