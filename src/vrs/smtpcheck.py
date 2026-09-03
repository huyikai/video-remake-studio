from __future__ import annotations

import smtplib
import ssl
from typing import Any


def smtp_enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("enabled"))


def check_smtp(cfg: dict[str, Any]) -> dict[str, Any]:
    if not smtp_enabled(cfg):
        return {"id": "smtp", "ok": True, "skipped": True, "detail": "未启用"}
    host = str(cfg.get("host") or "")
    port = int(cfg.get("port") or 0)
    user = str(cfg.get("user") or "")
    password = str(cfg.get("password") or "")
    security = str(cfg.get("security") or "starttls").lower()
    if not host or not port:
        return {"id": "smtp", "ok": False, "skipped": False, "detail": "缺少 host/port"}
    if not user or not password:
        return {"id": "smtp", "ok": False, "skipped": False, "detail": "缺少 user/password"}
    try:
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=8, context=context) as client:
                client.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=8) as client:
                if security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                elif security != "none":
                    return {
                        "id": "smtp",
                        "ok": False,
                        "skipped": False,
                        "detail": f"未知 security: {security}",
                    }
                client.login(user, password)
        return {"id": "smtp", "ok": True, "skipped": False, "detail": f"已登录 {host}:{port}"}
    except Exception as exc:  # noqa: BLE001 — 连通性检查要原样回报
        return {"id": "smtp", "ok": False, "skipped": False, "detail": str(exc)}
