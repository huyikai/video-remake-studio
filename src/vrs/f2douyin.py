"""抖音单条进料：f2 Python API 下视频和旁证，不弹浏览器。

Cookie 人手放环境变量或 local.yaml。图集/直播/动图直接失败。评论尽力，失败不影响视频。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from vrs.settings import Settings, load_yaml

import yaml

VIDEO_TYPES = {0, 4, 55, 61, 109, 201}
TYPE_LABELS = {
    68: "图集",
}
COMMENT_FETCH = 100
COMMENT_KEEP = 30
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class DouyinIngestError(RuntimeError):
    pass


def is_douyin_url(url: str) -> bool:
    low = (url or "").lower()
    return any(s in low for s in ("douyin.com", "iesdouyin.com"))


def resolve_cookie(settings: Settings) -> str:
    env = (os.environ.get("VRS_DOUYIN_COOKIE") or "").strip()
    cfg = str((settings.default.get("douyin") or {}).get("cookie") or "").strip()
    cookie = env or cfg
    if cookie and any(ord(ch) > 127 for ch in cookie):
        raise DouyinIngestError("抖音 Cookie 含非 ASCII 字符，f2 会直接拒。请从 Chrome 重新复制")
    return cookie


def cookie_ready(settings: Settings) -> tuple[bool, str]:
    try:
        cookie = resolve_cookie(settings)
    except DouyinIngestError as exc:
        return False, str(exc)
    if cookie:
        return True, f"已配置（{len(cookie)} 字符）"
    return False, "未配置抖音 Cookie：请到设置里粘贴，或设环境变量 VRS_DOUYIN_COOKIE"


_AUTH_MARKERS = (
    "需要登录 Cookie",
    "未配置抖音 Cookie",
    "Cookie 过期",
    "Cookie 失效",
    "Cookie 无效",
    "VRS_DOUYIN_COOKIE",
)


def is_douyin_auth_error(exc: Any) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in _AUTH_MARKERS)


def cookie_expired(settings: Settings) -> bool:
    status = str(settings.default.get("douyin_cookie_status") or "ok").lower()
    if status == "expired":
        return True
    runtime = load_yaml(settings.root / "data" / "runtime.yaml")
    return str(runtime.get("douyin_cookie_status") or "").lower() == "expired"


def set_cookie_expired(settings: Settings, expired: bool) -> None:
    path = settings.root / "data" / "runtime.yaml"
    runtime = load_yaml(path) if path.is_file() else {}
    runtime["douyin_cookie_status"] = "expired" if expired else "ok"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    settings.reload()


def _kwargs(cookie: str, settings: Settings) -> dict[str, Any]:
    cfg = settings.default.get("douyin") or {}
    timeout = _cfg_int(settings, "timeout", 10)
    return {
        "headers": {"User-Agent": _UA, "Referer": "https://www.douyin.com/"},
        "proxies": {"http://": None, "https://": None},
        "cookie": cookie,
        "music": True,
        "cover": True,
        "desc": True,
        "folderize": False,
        "naming": "{aweme_id}",
        "interval": "all",
        "timeout": timeout,
        "max_retries": _cfg_int(settings, "max_retries", 5),
        "max_connections": int(cfg.get("max_connections") or 5),
        "max_tasks": int(cfg.get("max_tasks") or 10),
    }


def _cfg_int(settings: Settings, key: str, default: int) -> int:
    raw = (settings.default.get("douyin") or {}).get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _move_one(source: Path, pattern: str, dest: Path) -> Path | None:
    found = [
        p
        for p in source.rglob(pattern)
        if p.is_file() and p.resolve() != dest.resolve()
    ]
    if not found:
        if dest.is_file():
            return dest
        return None
    chosen = max(found, key=lambda p: p.stat().st_size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    chosen.replace(dest)
    return dest


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _not_a_video(detail: str) -> DouyinIngestError:
    return DouyinIngestError(f"不是单条视频（{detail}）")


def _topics(raw: Any) -> list[str]:
    tags: Any = raw
    if isinstance(tags, str):
        text = tags.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            tags = parsed if isinstance(parsed, list) else ([text] if text else [])
        else:
            tags = [text] if text else []
    if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str):
        inner = tags[0].strip()
        if inner.startswith("["):
            try:
                parsed = json.loads(inner)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                tags = parsed
    return [str(t).strip() for t in (tags or []) if str(t).strip()]


def _video_field(video: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if hasattr(video, name):
            value = getattr(video, name)
            if value not in (None, ""):
                return value
    return default


def _compact(url: str, aweme_id: str, video: Any) -> dict[str, Any]:
    tags = _topics(_video_field(video, "hashtag_names", default=[]))
    desc = _video_field(video, "desc_raw", "desc")
    caption = _video_field(video, "caption_raw", "caption")
    return {
        "via": "f2",
        "url": url,
        "aweme_id": aweme_id,
        "title": caption or desc,
        "description": desc,
        "author": _video_field(video, "nickname_raw", "nickname"),
        "author_id": _video_field(video, "unique_id", default=None),
        "topics": tags,
        "stats": {
            "like": _video_field(video, "digg_count", default=None),
            "comment": _video_field(video, "comment_count", default=None),
            "share": _video_field(video, "share_count", default=None),
            "collect": _video_field(video, "collect_count", default=None),
        },
        "create_time": _video_field(video, "create_time", default=None),
        "aweme_type": _video_field(video, "aweme_type", default=None),
        "skipped": False,
    }


def _comment_row(item: dict[str, Any]) -> dict[str, Any] | None:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    text = str(
        item.get("comment_text_raw")
        or item.get("comment_text")
        or item.get("text")
        or ""
    ).strip()
    if not text:
        return None
    likes = _as_int(item.get("digg_count")) or 0
    return {
        "cid": item.get("cid") or item.get("comment_id"),
        "text": text,
        "like": likes,
        "author": item.get("nickname_raw") or item.get("nickname") or user.get("nickname"),
        "create_time": item.get("create_time"),
        "is_hot": item.get("is_hot"),
    }


def _rank_comments(items: list[Any], keep: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _comment_row(item)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (-int(r["like"] or 0), str(r.get("cid") or "")))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row.get("cid") or row["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= keep:
            break
    return out


def _has_video_file(dest_dir: Path) -> bool:
    if (dest_dir / "video.mp4").is_file():
        return True
    return any(dest_dir.rglob("*_video.mp4"))


async def _fetch_comments(
    kw: dict[str, Any], aweme_id: str, fetch_n: int
) -> tuple[list[dict[str, Any]], str | None]:
    """PyPI 版 f2 没有 Handler.fetch_post_comment，走 Crawler。失败只警告。"""
    try:
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin.model import PostComment
    except ImportError as exc:
        return [], f"此版本 f2 没有评论接口：{exc}"

    raw: list[dict[str, Any]] = []
    cursor = 0
    try:
        async with DouyinCrawler(kw) as crawler:
            while len(raw) < fetch_n:
                count = min(20, fetch_n - len(raw))
                params = PostComment(
                    aweme_id=str(aweme_id),
                    cursor=cursor,
                    count=count,
                    insert_ids="",
                    whale_cut_token="",
                    rcFT="",
                )
                response = await crawler.fetch_post_comment(params)
                if not isinstance(response, dict):
                    break
                batch = response.get("comments") or []
                if not isinstance(batch, list) or not batch:
                    break
                raw.extend(item for item in batch if isinstance(item, dict))
                if not response.get("has_more"):
                    break
                nxt = _as_int(response.get("cursor"))
                if nxt is None or nxt == cursor:
                    break
                cursor = nxt
                await asyncio.sleep(1)
    except Exception as exc:  # noqa: BLE001
        return raw, f"评论拉取失败：{exc}"
    return raw, None


async def _ingest(url: str, dest_dir: Path, settings: Settings) -> dict[str, Any]:
    try:
        from f2.apps.douyin.handler import DouyinHandler
        from f2.apps.douyin.utils import AwemeIdFetcher
    except ImportError as exc:
        raise DouyinIngestError("未安装 f2，请执行 uv sync") from exc

    low = (url or "").lower()
    if "live.douyin.com" in low or "/live/" in low:
        raise _not_a_video("直播链接")

    cookie = resolve_cookie(settings)
    if not cookie:
        raise DouyinIngestError(
            "抖音下载需要登录 Cookie：请到设置里粘贴，或设环境变量 VRS_DOUYIN_COOKIE"
        )
    kw = _kwargs(cookie, settings)
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        aweme_id = await AwemeIdFetcher.get_aweme_id(url)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "aweme_id" in msg.lower() or "不支持" in msg:
            raise _not_a_video("无法解析作品 ID，图集/直播请换链接") from exc
        raise DouyinIngestError(f"无法从链接解析作品 ID：{exc}") from exc

    handler = DouyinHandler(kw)
    handler.enable_bark = False
    try:
        video = await handler.fetch_one_video(aweme_id)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "nickname" in text.lower():
            raise DouyinIngestError("抖音详情失败。Cookie 过期或失效。") from exc
        if "动图" in text:
            raise _not_a_video("动图") from exc
        raise DouyinIngestError(
            "抖音详情失败。Cookie 过期或失效时请重新导入登录态。"
            f" 原始错误：{exc}"
        ) from exc

    if video.nickname is None:
        raise DouyinIngestError("抖音详情失败。Cookie 过期或失效。")
    aweme_type = _as_int(video.aweme_type)
    if aweme_type is None:
        aweme_type = video.aweme_type
    if aweme_type not in VIDEO_TYPES:
        kind = TYPE_LABELS.get(aweme_type, f"aweme_type={aweme_type}")
        raise _not_a_video(f"{kind}，请换一条普通视频链接")
    if not video.video_play_addr:
        raise DouyinIngestError("详情里没有播放地址，无法下载")

    aweme = video._to_dict()
    if aweme.get("private_status") not in (0, 1, 2):
        aweme["private_status"] = 0
    try:
        raw = video._to_raw()
    except Exception:  # noqa: BLE001
        raw = aweme
    _write_json(dest_dir / "f2_aweme.json", raw)

    try:
        await handler.downloader.create_download_tasks(kw, aweme, dest_dir)
        if not _has_video_file(dest_dir) and video.video_play_addr:
            await handler.downloader.initiate_download(
                "视频",
                video.video_play_addr,
                dest_dir,
                f"{aweme_id}_video",
                ".mp4",
            )
            await handler.downloader.execute_tasks()
    finally:
        close = getattr(handler.downloader, "close", None)
        if close:
            with contextlib.suppress(Exception):
                await close()

    video_path = _move_one(dest_dir, "*_video.mp4", dest_dir / "video.mp4")
    if video_path is None:
        raise DouyinIngestError("f2 没有产出 mp4")
    _move_one(dest_dir, "*_cover.jpeg", dest_dir / "cover.jpg")
    _move_one(dest_dir, "*_cover.jpg", dest_dir / "cover.jpg")
    _move_one(dest_dir, "*_cover.webp", dest_dir / "cover.webp")
    _move_one(dest_dir, "*_music.mp3", dest_dir / "music.mp3")
    _move_one(dest_dir, "*_desc.txt", dest_dir / "desc.txt")

    fetch_n = _cfg_int(settings, "comment_fetch", COMMENT_FETCH)
    keep_n = _cfg_int(settings, "comment_keep", COMMENT_KEEP)
    raw_comments, warning = await _fetch_comments(kw, str(aweme_id), fetch_n)
    kept = _rank_comments(raw_comments, keep_n)
    fetched = len(raw_comments)
    expected = _as_int(video.comment_count) or 0
    if warning is None and expected > 0 and not kept:
        warning = "评论接口没有返回条目（关评或风控）"
    _write_json(
        dest_dir / "comments.json",
        {"fetched": fetched, "kept": len(kept), "warning": warning, "comments": kept},
    )

    meta = _compact(url, str(aweme_id), video)
    if warning:
        meta["comment_warning"] = warning
    meta["comments_file"] = "source/comments.json"
    _write_json(dest_dir / "page_meta.json", meta)

    for junk in dest_dir.glob("*.db"):
        junk.unlink(missing_ok=True)
    return meta


def download_douyin(settings: Settings, url: str, dest_dir: Path) -> dict[str, Any]:
    """同步入口。落盘 source/ 下的固定文件名。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.chdir(dest_dir):
        return asyncio.run(_ingest(url, dest_dir, settings))
