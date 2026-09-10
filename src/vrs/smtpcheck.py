from __future__ import annotations

import smtplib
from typing import Any


def smtp_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enabled"))


def smtp_credentials_ready(cfg: dict[str, Any]) -> bool:
    return bool(str(cfg.get("user") or "").strip() and str(cfg.get("password") or "").strip())


def check_smtp(cfg: dict[str, Any]) -> dict[str, Any]:
    """只看配置齐不齐，不向邮件服务器登录。"""
    if not smtp_enabled(cfg):
        return {"id": "smtp", "ok": True, "skipped": True, "detail": "未启用"}
    user = str(cfg.get("user") or "").strip()
    password = str(cfg.get("password") or "").strip()
    missing: list[str] = []
    if not user:
        missing.append("邮箱")
    if not password:
        missing.append("授权码")
    if missing:
        return {
            "id": "smtp",
            "ok": False,
            "skipped": False,
            "detail": "未配置" + "或".join(missing),
        }
    return {"id": "smtp", "ok": True, "skipped": False, "detail": f"已配置 {user}"}


def format_smtp_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    extra = getattr(exc, "smtp_error", "") or ""
    if isinstance(extra, bytes):
        extra = extra.decode("utf-8", "replace")
    extra = str(extra).strip()
    code = getattr(exc, "smtp_code", None)
    blob = f"{text} {extra}"
    if code == 535 or "535" in blob or "Login fail" in blob:
        body = extra or text
        return f"QQ 拒绝登录（535）：{body}"
    if "unexpectedly closed" in text.lower() or isinstance(exc, smtplib.SMTPServerDisconnected):
        return "登录时连接被断开（授权码错误、SMTP 未开启或登录太频繁）。不是没填授权码。"
    return text
