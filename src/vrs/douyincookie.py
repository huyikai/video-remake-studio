"""从本机 Chrome 导入抖音 Cookie。不把 Cookie 写进日志。

网页自己读不了 douyin.com 的登录态（同源策略，且 sessionid 常是 HttpOnly）。
Chrome 127+ 也禁止外部程序解密 Cookie 数据库。可行的两条路：

1. 连上带远程调试端口的 Chrome（已登录的那个），直接读 Cookie。
2. 弹出独立 Chrome 窗口，扫码登录一次，登录态记在 data/browser/douyin。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from vrs.settings import Settings

_DOUYIN_URLS = ("https://www.douyin.com/", "https://www.iesdouyin.com/")
_LOGIN_MARKERS = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt"}
_PROFILE = Path("data") / "browser" / "douyin"


class DouyinCookieError(RuntimeError):
    pass


def _browser_cfg(settings: Settings) -> dict[str, Any]:
    return settings.default.get("browser") or {}


def _is_douyin_domain(domain: str) -> bool:
    host = (domain or "").lstrip(".").lower()
    return host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(
        ".iesdouyin.com"
    )


def cookies_to_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        if not _is_douyin_domain(str(cookie.get("domain") or "")):
            continue
        name = str(cookie.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def looks_logged_in(header: str) -> bool:
    names = {item.split("=", 1)[0].strip() for item in header.split(";") if "=" in item}
    return bool(names & _LOGIN_MARKERS)


def _header_from_contexts(contexts: Any) -> str:
    collected: list[dict[str, Any]] = []
    for ctx in contexts:
        try:
            collected.extend(ctx.cookies(list(_DOUYIN_URLS)))
        except Exception:
            try:
                collected.extend(ctx.cookies())
            except Exception:
                continue
    return cookies_to_header(collected)


def try_cdp(settings: Settings) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    cdp = str(_browser_cfg(settings).get("cdp_url") or "http://127.0.0.1:9222")
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp)
    except Exception as exc:
        playwright.stop()
        raise DouyinCookieError(
            "当前 Chrome 没有开远程调试，连不上已登录的那个窗口。"
            f"（{cdp}）"
        ) from exc
    try:
        header = _header_from_contexts(browser.contexts)
        if looks_logged_in(header):
            return header, "cdp"
        raise DouyinCookieError("已连上 Chrome，但没有抖音登录 Cookie。请先在这个 Chrome 里打开 douyin.com 并登录。")
    finally:
        playwright.stop()


def grab_via_login_window(settings: Settings, *, timeout_sec: float = 180.0) -> tuple[str, str]:
    from playwright.sync_api import sync_playwright

    profile = (settings.root / _PROFILE).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    channel = str(_browser_cfg(settings).get("channel") or "chrome")
    playwright = sync_playwright().start()
    context = None
    try:
        launch: dict[str, Any] = {
            "headless": False,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "viewport": {"width": 1280, "height": 800},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = playwright.chromium.launch_persistent_context(str(profile), channel=channel, **launch)
        except Exception:
            context = playwright.chromium.launch_persistent_context(str(profile), **launch)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(_DOUYIN_URLS[0], wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + max(30.0, timeout_sec)
        header = ""
        while time.monotonic() < deadline:
            header = cookies_to_header(context.cookies(list(_DOUYIN_URLS)))
            if looks_logged_in(header):
                return header, "login_window"
            page.wait_for_timeout(800)
        if looks_logged_in(header):
            return header, "login_window"
        raise DouyinCookieError("等待登录超时。请在弹出的窗口里扫码登录后再点一次「一键导入」。")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        playwright.stop()


def grab_douyin_cookie(settings: Settings, *, force_window: bool = False) -> tuple[str, str]:
    """返回 (cookie_header, source)。失败抛 DouyinCookieError。"""
    if not force_window:
        try:
            return try_cdp(settings)
        except DouyinCookieError:
            pass
    return grab_via_login_window(settings)
