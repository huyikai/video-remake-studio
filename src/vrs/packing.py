"""把事件表打包成 H3 能生成的片段。

事件是故事单位，所以**只拆不并**：两个事件合进同一次生成，等于把故事边界埋进
一条片子里，硬切拼接就再也切不开。超过 H3 上限的事件按规则候选切点二次拆分。

原片里一段故事常常是两拍（换装、换景、硬切），每拍只有 2–4 秒。H3 下限约 4.5s，
短拍用 pad 补时长，不能把「两边都够 4.5s」当成源片最小切块——那样 6.5s 的故事
切不开，过去和现在会写进同一条。
"""

from __future__ import annotations

from typing import Any

from vrs.h3grid import snap_seconds, t_bounds
from vrs.passa import cut_score

# 源片单拍短于这个，多半是误切残片；生成时再 pad 到 H3 下限
SHOT_MIN = 2.0
SCENE_REASONS = {"画面硬切", "片尾硬切"}


def _round(t: float) -> float:
    return round(float(t), 2)


def _has_scene(reasons: list[str]) -> bool:
    return bool(SCENE_REASONS & set(reasons or []))


def pick_split(
    t0: float,
    t1: float,
    candidates: list[dict[str, Any]],
    *,
    t_min: float,
    scene_only: bool = False,
) -> float | None:
    """在 [t0+t_min, t1-t_min] 里挑一刀。取信号最强的，同分取更靠中间的。

    这里用全量候选而不是短名单：短名单是为「哪里是故事边界」筛的，
    段内二次拆分要的是「哪里画面/对白允许断开」，两回事。
    """
    lo, hi = t0 + t_min, t1 - t_min
    if hi < lo:
        return None
    mid = (t0 + t1) / 2
    inside = [c for c in candidates if lo - 1e-6 <= float(c["t"]) <= hi + 1e-6]
    if scene_only:
        inside = [c for c in inside if _has_scene(list(c.get("reasons") or []))]
    if not inside:
        return None
    best = max(
        inside,
        key=lambda c: (
            1 if _has_scene(list(c.get("reasons") or [])) else 0,
            cut_score(list(c.get("reasons") or [])),
            -abs(float(c["t"]) - mid),
        ),
    )
    return _round(float(best["t"]))


def split_span(
    t0: float,
    t1: float,
    candidates: list[dict[str, Any]],
    *,
    t_min: float,
    t_max: float,
) -> list[tuple[float, float]]:
    """先按画面硬切拆成单拍；仍超过 H3 上限的再按任意候选切，切不动就均分。"""
    cut = pick_split(t0, t1, candidates, t_min=t_min, scene_only=True)
    if cut is not None:
        left = split_span(t0, cut, candidates, t_min=t_min, t_max=t_max)
        right = split_span(cut, t1, candidates, t_min=t_min, t_max=t_max)
        return left + right
    if t1 - t0 <= t_max + 1e-6:
        return [(t0, t1)]
    cut = pick_split(t0, t1, candidates, t_min=t_min, scene_only=False)
    if cut is None:
        n = int((t1 - t0) // t_max) + 1
        step = (t1 - t0) / n
        return [(_round(t0 + i * step), _round(t0 + (i + 1) * step)) for i in range(n)]
    left = split_span(t0, cut, candidates, t_min=t_min, t_max=t_max)
    right = split_span(cut, t1, candidates, t_min=t_min, t_max=t_max)
    return left + right


def pack_h3_clips(
    events: list[dict[str, Any]],
    *,
    settings: Any,
    candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    h3_min, t_max = t_bounds(settings)
    cands = list(candidates or [])
    clips: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("kind") or "story") == "endcard":
            continue
        t0, t1 = float(event["t0"]), float(event["t1"])
        parts = split_span(t0, t1, cands, t_min=SHOT_MIN, t_max=t_max)
        for i, (part_t0, part_t1) in enumerate(parts):
            source = part_t1 - part_t0
            frames, snapped = snap_seconds(min(max(source, h3_min), t_max), settings)
            clip: dict[str, Any] = {
                "id": f"h3_{len(clips) + 1:02d}",
                "event_id": event.get("id") or "",
                "kind": event.get("kind") or "story",
                "t0": _round(part_t0),
                "t1": _round(part_t1),
                "source_seconds": _round(source),
                "h3_frames": frames,
                "h3_seconds": round(snapped, 3),
                "drift": round(snapped - source, 3),
                "padded": source < h3_min - 1e-6,
                "cast_reset": bool(event.get("cast_reset")) and i == 0,
            }
            if len(parts) > 1:
                clip["split_from"] = event.get("id") or ""
            clips.append(clip)
    return clips
