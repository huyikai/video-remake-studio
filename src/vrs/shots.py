from __future__ import annotations

from pathlib import Path
from typing import Any

from vrs.probe import probe_video


def detect_shots(video: Path, *, threshold: float = 27.0, min_seconds: float = 0.4) -> list[dict[str, Any]]:
    info = probe_video(video)
    duration = float(info["duration"])
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as exc:
        raise RuntimeError("未安装 scenedetect，请执行 uv sync --extra asr") from exc

    scenes = detect(
        str(video),
        ContentDetector(threshold=threshold),
        start_in_scene=True,
        show_progress=False,
    )
    raw: list[tuple[float, float]] = []
    if not scenes:
        raw = [(0.0, duration)]
    else:
        for start, end in scenes:
            t0 = float(start.get_seconds())
            t1 = float(end.get_seconds())
            if t1 > t0 + 1e-3:
                raw.append((t0, min(t1, duration)))
        if raw and raw[-1][1] < duration - 0.05:
            raw.append((raw[-1][1], duration))
        if not raw:
            raw = [(0.0, duration)]

    merged: list[tuple[float, float]] = []
    for t0, t1 in raw:
        if (t1 - t0) < min_seconds and merged:
            prev0, _ = merged.pop()
            merged.append((prev0, t1))
        else:
            merged.append((t0, t1))
    if len(merged) >= 2 and (merged[-1][1] - merged[-1][0]) < min_seconds:
        t0, t1 = merged.pop()
        prev0, _ = merged.pop()
        merged.append((prev0, t1))

    shots: list[dict[str, Any]] = []
    for index, (t0, t1) in enumerate(merged, start=1):
        shots.append({"id": f"shot_{index:03d}", "t0": round(t0, 3), "t1": round(t1, 3)})
    return shots
