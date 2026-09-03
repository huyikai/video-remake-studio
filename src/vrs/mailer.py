"""可选 SMTP。失败只记日志，不改变 Job 状态。"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from vrs.smtpcheck import smtp_enabled


def _log(log: Callable[[str], None] | None, text: str) -> None:
    if log:
        log(text)


def send_mail(
    cfg: dict[str, Any],
    *,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    if not smtp_enabled(cfg):
        return
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
        _log(log, "SMTP 已启用但缺少 host/from/to，跳过")
        return
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
    try:
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as client:
                if user and password:
                    client.login(user, password)
                client.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as client:
                if security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if user and password:
                    client.login(user, password)
                client.send_message(msg)
        _log(log, f"已发信：{subject}")
    except Exception as exc:  # noqa: BLE001
        _log(log, f"发信失败（任务状态不变）：{exc}")
