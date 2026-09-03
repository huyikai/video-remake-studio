from __future__ import annotations

from typing import Any

from vrs.settings import Settings

GENERATE_PATHS: tuple[str, ...] = ("i2va_turbo", "t2va_turbo", "ref2va")

# 早期把 I2VA 工作流错标成了 fl2va_turbo，旧 job.json 里还留着这个名字
PATH_ALIASES: dict[str, str] = {"fl2va_turbo": "i2va_turbo"}

# 走哪条路决定提示词首行指令，也决定要不要抽关键帧
PATH_KEYFRAMES: dict[str, int] = {"i2va_turbo": 1, "t2va_turbo": 0, "ref2va": 0}

# T2VA 不跨段锁脸：情节、对白一致即可，人物不必长得一样
PATH_LOCK_ACROSS: dict[str, bool] = {"i2va_turbo": True, "t2va_turbo": False, "ref2va": True}


def normalize_generate_path(name: str) -> str:
    path = PATH_ALIASES.get(str(name), str(name))
    if path not in GENERATE_PATHS:
        raise ValueError(f"未知的生成路线 {name!r}，可选 {', '.join(GENERATE_PATHS)}")
    return path


def h3_cfg(settings: Settings) -> dict[str, Any]:
    return {
        "fps": float(settings.h3.get("fps") or 24),
        "frame_mod": int(settings.h3.get("frame_mod") or 17),
        "frame_remainder": int(settings.h3.get("frame_remainder") or 5),
        "frame_min": int(settings.h3.get("frame_min") or 107),
        "frame_max": int(settings.h3.get("frame_max") or 345),
    }


def legal_frame_set(settings: Settings) -> list[int]:
    cfg = h3_cfg(settings)
    return [
        n
        for n in range(cfg["frame_min"], cfg["frame_max"] + 1)
        if n % cfg["frame_mod"] == cfg["frame_remainder"]
    ]


def t_bounds(settings: Settings) -> tuple[float, float]:
    cfg = h3_cfg(settings)
    fps = cfg["fps"]
    return cfg["frame_min"] / fps, cfg["frame_max"] / fps


def seconds_to_frames(seconds: float, settings: Settings) -> int:
    fps = h3_cfg(settings)["fps"]
    return max(1, int(round(float(seconds) * fps)))


def snap_frames(n: int, settings: Settings) -> int:
    legal = legal_frame_set(settings)
    return min(legal, key=lambda item: (abs(item - n), item))


def snap_seconds(seconds: float, settings: Settings) -> tuple[int, float]:
    frames = snap_frames(seconds_to_frames(seconds, settings), settings)
    fps = h3_cfg(settings)["fps"]
    return frames, frames / fps
