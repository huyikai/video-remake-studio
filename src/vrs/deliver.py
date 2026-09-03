"""成片时间轴：H3 短拍会 pad 到网格下限，合剪时按源片这一拍从头裁掉多出来的保持。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vrs.media import concat_videos, trim_duration


def clip_play_seconds(clip: dict[str, Any]) -> float:
    source = float(clip.get("source_seconds") or 0)
    h3 = float(clip.get("h3_seconds") or source)
    if source <= 0.05:
        return h3
    return min(source, h3)


def concat_spans(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条 clip 在合剪轴上占源片时长（已裁 pad）。"""
    offset = 0.0
    out: list[dict[str, Any]] = []
    for clip in clips:
        play = clip_play_seconds(clip)
        t0 = float(clip["t0"])
        t1 = float(clip["t1"])
        out.append(
            {
                "id": clip.get("id"),
                "src0": t0,
                "src1": t1,
                "play": play,
                "cat0": round(offset, 3),
                "cat1": round(offset + play, 3),
            }
        )
        offset += play
    return out


def source_to_concat(t: float, spans: list[dict[str, Any]]) -> float | None:
    for span in spans:
        if span["src0"] - 1e-3 <= t <= span["src1"] + 1e-3:
            local = min(max(0.0, t - span["src0"]), span["play"])
            return span["cat0"] + local
    return None


def map_span(t0: float, t1: float, spans: list[dict[str, Any]]) -> tuple[float, float] | None:
    a = source_to_concat(t0, spans)
    b = source_to_concat(t1, spans)
    if a is None and b is None:
        return None
    if a is None:
        a = source_to_concat(max(t0, spans[0]["src0"]), spans) if spans else None
    if b is None:
        b = source_to_concat(min(t1, spans[-1]["src1"]), spans) if spans else None
    if a is None or b is None or b - a < 0.12:
        return None
    return (round(a, 3), round(b, 3))


def trim_and_concat(
    clips: list[dict[str, Any]],
    *,
    src_dir: Path,
    dest: Path,
    work_dir: Path,
    log_path: Path | None = None,
) -> list[Path]:
    """把每段裁到源片时长再硬切拼接。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    for clip in clips:
        src = src_dir / f"{clip['id']}.mp4"
        if not src.is_file():
            raise ProbeError(f"缺片段 {src}")
        play = clip_play_seconds(clip)
        h3 = float(clip.get("h3_seconds") or play)
        piece = work_dir / f"{clip['id']}.mp4"
        if play + 0.04 < h3:
            trim_duration(src, piece, play, log_path=log_path)
        else:
            piece.write_bytes(src.read_bytes())
        pieces.append(piece)
    concat_videos(pieces, dest, log_path=log_path)
    return pieces
