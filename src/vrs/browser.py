from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from vrs.settings import Settings

_COOKIE_SITES = ("tiktok.com",)
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def cookies_to_netscape(cookies: list[dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if not name:
            continue
        domain = str(cookie.get("domain") or "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        raw_exp = cookie.get("expires")
        try:
            expires = int(raw_exp) if raw_exp is not None and float(raw_exp) > 0 else 0
        except (TypeError, ValueError):
            expires = 0
        value = str(cookie.get("value") or "")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def write_cookies_file(path: Path, cookies: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cookies_to_netscape(cookies), encoding="utf-8")


def needs_browser_cookies(url: str) -> bool:
    low = url.lower()
    return any(site in low for site in _COOKIE_SITES)


def _browser_cfg(settings: Settings) -> dict[str, Any]:
    return settings.default.get("browser") or {}


def open_browser(settings: Settings, *, headed: bool | None = None):
    """返回 (playwright, browser, via)。via 为 cdp 时不要 close 用户的 Chrome。"""
    from playwright.sync_api import sync_playwright

    cfg = _browser_cfg(settings)
    playwright = sync_playwright().start()
    cdp = str(cfg.get("cdp_url") or "http://127.0.0.1:9222")
    try:
        browser = playwright.chromium.connect_over_cdp(cdp)
        return playwright, browser, "cdp"
    except Exception:
        pass
    channel = str(cfg.get("channel") or "chrome")
    headless = bool(cfg.get("headless", True)) if headed is None else not headed
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        browser = playwright.chromium.launch(channel=channel, **launch_kwargs)
        return playwright, browser, f"launch:{channel}"
    except Exception:
        browser = playwright.chromium.launch(**launch_kwargs)
        return playwright, browser, "launch:chromium"


def close_browser(playwright, browser, via: str) -> None:
    try:
        if via != "cdp":
            browser.close()
    finally:
        playwright.stop()


def _douyin_video_id(url: str) -> str | None:
    marker = "/video/"
    if marker not in url:
        return None
    video_id = url.split(marker, 1)[1].split("?")[0].strip("/")
    return video_id if video_id.isdigit() else None


def _candidate_urls(url: str) -> list[str]:
    video_id = _douyin_video_id(url)
    if not video_id or not needs_browser_cookies(url):
        return [url]
    ordered = [
        f"https://www.iesdouyin.com/share/video/{video_id}",
        url,
        f"https://m.douyin.com/share/video/{video_id}",
    ]
    seen: list[str] = []
    for item in ordered:
        if item not in seen:
            seen.append(item)
    return seen


def _page_looks_ready(extracted: dict[str, Any] | None) -> bool:
    extracted = extracted or {}
    title = str(extracted.get("title") or "").strip()
    body = str(extracted.get("body_preview") or "")
    if extracted.get("aweme"):
        return True
    if "抱歉出错了" in body:
        return True
    if "视频数据加载中" in body:
        return False
    if extracted.get("video_count"):
        return bool(title)
    return bool(title) and title not in {"抖音", "Douyin"}


def compact_aweme(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(detail, dict):
        return None
    node = detail.get("aweme_detail") if isinstance(detail.get("aweme_detail"), dict) else None
    if node is None and "desc" in detail and isinstance(detail.get("statistics"), dict):
        node = detail
    if not isinstance(node, dict):
        return None
    author_obj = node.get("author") if isinstance(node.get("author"), dict) else {}
    stats = node.get("statistics") if isinstance(node.get("statistics"), dict) else {}
    return {
        "desc": node.get("desc"),
        "author": {
            "nickname": author_obj.get("nickname"),
            "unique_id": author_obj.get("unique_id"),
        },
        "statistics": {
            "digg_count": stats.get("digg_count"),
            "comment_count": stats.get("comment_count"),
            "share_count": stats.get("share_count"),
            "collect_count": stats.get("collect_count"),
            "play_count": stats.get("play_count"),
        },
    }


def play_urls_from_detail(detail: dict[str, Any] | None) -> list[str]:
    if not isinstance(detail, dict):
        return []
    video = (detail.get("aweme_detail") or {}).get("video") if isinstance(detail.get("aweme_detail"), dict) else None
    if not isinstance(video, dict):
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    urls: list[str] = []

    def collect(addr: Any) -> None:
        if isinstance(addr, dict):
            for item in addr.get("url_list") or []:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)

    collect(video.get("play_addr"))
    collect(video.get("play_addr_h264"))
    collect(video.get("download_addr"))
    for bit in video.get("bit_rate") or []:
        if isinstance(bit, dict):
            collect(bit.get("play_addr"))
    seen: list[str] = []
    for url in urls:
        if url not in seen:
            seen.append(url)
    return seen


def pick_play_urls(urls: list[str], *, limit: int = 4) -> list[str]:
    vod = [
        u
        for u in urls
        if "douyinvod.com" in u and "watermark=1" not in u
    ]
    rest = [u for u in urls if u not in vod and "watermark=1" not in u]
    return (vod + rest)[:limit]


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for cookie in cookies:
        name = cookie.get("name")
        if name:
            parts.append(f"{name}={cookie.get('value') or ''}")
    return "; ".join(parts)


def stream_play_url(
    url: str,
    dest: Path,
    *,
    cookies: list[dict[str, Any]],
    timeout: float = 180.0,
) -> int:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.unlink(missing_ok=True)
    headers = {
        "User-Agent": _CHROME_UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    cookie_header = _cookie_header(cookies)
    if cookie_header:
        headers["Cookie"] = cookie_header
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            first = True
            written = 0
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes(256 * 1024):
                    if not chunk:
                        continue
                    if first:
                        head = chunk.lstrip()[:16]
                        if head.startswith(b"<") or head.startswith(b"{") or head.startswith(b"["):
                            raise RuntimeError("播放地址返回的不是视频")
                        first = False
                    handle.write(chunk)
                    written += len(chunk)
    if written < 100_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"播放地址文件过小：{written} 字节")
    tmp.replace(dest)
    return written


_EXTRACT_JS = """() => {
  const attr = (sel, name) => document.querySelector(sel)?.getAttribute(name) || null;
  const text = (sel) => document.querySelector(sel)?.textContent || null;
  return {
    title: attr('meta[property="og:title"]', 'content') || document.title || null,
    description: attr('meta[property="og:description"]', 'content')
      || attr('meta[name="description"]', 'content'),
    canonical: attr('meta[property="og:url"]', 'content') || location.href,
    site: attr('meta[property="og:site_name"]', 'content'),
    h1: text('h1'),
    video_count: document.querySelectorAll('video').length,
    body_preview: (document.body && document.body.innerText || '').slice(0, 400),
  };
}"""


def _download_via_context(context, url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    response = context.request.get(
        url,
        headers={"Referer": "https://www.douyin.com/", "User-Agent": _CHROME_UA},
        timeout=180_000,
    )
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")
    data = response.body()
    try:
        head = data[:16].lstrip()
        if head.startswith((b"<", b"{", b"[")):
            raise RuntimeError("播放地址返回的不是视频")
        if len(data) < 100_000:
            raise RuntimeError(f"播放地址文件过小：{len(data)} 字节")
        dest.write_bytes(data)
        return len(data)
    finally:
        del data


def visit_and_collect(
    settings: Settings,
    url: str,
    *,
    download_to: Path | None = None,
) -> dict[str, Any]:
    timeout = int((_browser_cfg(settings).get("timeout_ms") or 60000))
    headed = needs_browser_cookies(url)
    playwright, browser, via = open_browser(settings, headed=True if headed else None)
    captured: dict[str, Any] = {}
    downloaded_bytes = 0
    try:
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 720},
                user_agent=_CHROME_UA,
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
        page = context.new_page()

        def on_response(resp) -> None:
            if "/aweme/v1/web/aweme/detail" not in resp.url or resp.status != 200:
                return
            if captured.get("detail"):
                return
            try:
                captured["detail"] = resp.json()
            except Exception:
                try:
                    captured["detail"] = json.loads(resp.text())
                except Exception:
                    return

        page.on("response", on_response)
        extracted: dict[str, Any] = {}
        final_url = url
        wait_s = max(8.0, min(timeout / 1000, 50.0))
        for target in _candidate_urls(url):
            page.goto(target, wait_until="domcontentloaded", timeout=timeout)
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and not captured.get("detail"):
                page.wait_for_timeout(400)
            extracted = page.evaluate(_EXTRACT_JS)
            final_url = page.url
            aweme = compact_aweme(captured.get("detail"))
            if aweme:
                extracted["aweme"] = aweme
            if captured.get("detail") or _page_looks_ready(extracted):
                break
        cookies = context.cookies()
        download_error = None
        if download_to is not None:
            for play in pick_play_urls(play_urls_from_detail(captured.get("detail"))):
                try:
                    downloaded_bytes = _download_via_context(context, play, download_to)
                    download_error = None
                    break
                except Exception as exc:
                    download_error = str(exc)
                    try:
                        downloaded_bytes = stream_play_url(play, download_to, cookies=cookies)
                        download_error = None
                        break
                    except Exception as exc2:
                        download_error = str(exc2)
                        download_to.unlink(missing_ok=True)
                        continue
        result = {
            "via": via,
            "extracted": extracted,
            "cookies": cookies,
            "final_url": final_url,
            "downloaded_bytes": downloaded_bytes,
        }
        if download_error and not downloaded_bytes:
            result["download_error"] = download_error
        return result
    finally:
        close_browser(playwright, browser, via)
