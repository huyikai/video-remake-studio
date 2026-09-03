"""原片画幅 vs 默认 16:9 输出。"""

from __future__ import annotations

from math import gcd

STANDARDS = (
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "1:1",
    "21:9",
    "3:2",
    "2:3",
)


def _parse(label: str) -> float:
    a, b = label.split(":")
    return float(a) / float(b)


def aspect_label(width: int, height: int) -> str:
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return "16:9"
    ratio = w / h
    best = min(STANDARDS, key=lambda item: abs(ratio - _parse(item)))
    if abs(ratio - _parse(best)) <= 0.04:
        return best
    g = gcd(w, h)
    return f"{w // g}:{h // g}"


def aspect_mismatch(width: int | None, height: int | None, default: str) -> tuple[str | None, bool]:
    if not width or not height:
        return None, False
    label = aspect_label(int(width), int(height))
    return label, label != str(default or "16:9")
