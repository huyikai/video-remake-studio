"""封面：配置了文生图就调一次，否则用成片第一帧。失败不挡交付。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx

from vrs.media import extract_frame_at


def _log(log: Callable[[str], None] | None, text: str) -> None:
    if log:
        log(text)


def write_cover(
    settings: Any,
    dest: Path,
    *,
    video: Path,
    title: str = "",
    log: Callable[[str], None] | None = None,
) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cfg = dict(settings.providers.get("t2i") or {})
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    key = str(cfg.get("api_key") or "")
    if base and model:
        prompt = (
            "Cinematic 16:9 movie poster still, photorealistic, no text, no logos, no watermark. "
            + (title[:120] if title else "short-form live-action drama")
        )
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    f"{base}/images/generations",
                    headers=headers,
                    json={"model": model, "prompt": prompt, "size": "1792x1024", "n": 1},
                )
                response.raise_for_status()
                payload = response.json()
            url = (((payload.get("data") or [{}])[0]).get("url")) or ""
            b64 = (((payload.get("data") or [{}])[0]).get("b64_json")) or ""
            if url:
                with httpx.Client(timeout=60) as client:
                    raw = client.get(url)
                    raw.raise_for_status()
                    dest.write_bytes(raw.content)
                    _log(log, f"封面文生图 {dest}")
                    return "t2i"
            if b64:
                import base64

                dest.write_bytes(base64.b64decode(b64))
                _log(log, f"封面文生图 {dest}")
                return "t2i"
        except Exception as exc:  # noqa: BLE001
            _log(log, f"封面文生图失败，改用成片首帧：{exc}")
    extract_frame_at(video, dest, 0.12, log_path=None)
    _log(log, f"封面用成片首帧 {dest}")
    return "frame"
