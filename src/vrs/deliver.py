"""成片时间轴：H3 短拍会 pad 到网格下限，合剪时按源片这一拍从头裁掉多出来的保持。"""

from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from vrs.media import concat_videos, trim_duration


def clip_play_seconds(clip: dict[str, Any], override: float | None = None) -> float:
    if override is not None and override > 0.05:
        return min(override, float(clip.get("h3_seconds") or override))
    source = float(clip.get("source_seconds") or 0)
    h3 = float(clip.get("h3_seconds") or source)
    if source <= 0.05:
        return h3
    return min(source, h3)


def _play_overrides(directory: Path | None, quality: str) -> dict[str, float]:
    """generate.json 里记录的动态裁剪线（H3 语速说不进源片窗口时延长），按档位取。"""
    if directory is None:
        return {}
    try:
        gen = json.loads((directory / "generate.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, float] = {}
    for cid, rec in (gen.get("clips") or {}).items():
        if isinstance(rec, dict) and isinstance(rec.get(quality), dict):
            play = rec[quality].get("play")
            if play:
                out[str(cid)] = float(play)
    return out


def concat_spans(
    clips: list[dict[str, Any]],
    overrides: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """每条 clip 在合剪轴上占源片时长（已裁 pad）；有动态裁剪线的段用延长值。"""
    overrides = overrides or {}
    offset = 0.0
    out: list[dict[str, Any]] = []
    for clip in clips:
        play = clip_play_seconds(clip, overrides.get(str(clip.get("id"))))
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
    master: bool = False,
    overrides: dict[str, float] | None = None,
    quality: str = "final",
) -> list[Path]:
    """把每段裁到源片时长再硬切拼接。

    master=True 时（仅 finish 成片链路用）每段音频先做响度归一 + 塌单声。
    overrides 未显式给时按 quality 从 generate.json 读动态裁剪线。"""
    if overrides is None:
        # src_dir = <job>/generate/{path}/{quality} → job 目录要三层 parent
        overrides = _play_overrides(src_dir.parent.parent.parent, quality)
    work_dir.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    for clip in clips:
        src = src_dir / f"{clip['id']}.mp4"
        if not src.is_file():
            raise ProbeError(f"缺片段 {src}")
        play = clip_play_seconds(clip, overrides.get(str(clip["id"])))
        h3 = float(clip.get("h3_seconds") or play)
        piece = work_dir / f"{clip['id']}.mp4"
        if play + 0.04 < h3:
            trim_duration(src, piece, play, log_path=log_path)
        else:
            piece.write_bytes(src.read_bytes())
        if master:
            from vrs.audio import normalize_piece_audio

            normalize_piece_audio(piece, piece, play, log_path=log_path)
        pieces.append(piece)
    concat_videos(pieces, dest, log_path=log_path)
    return pieces
