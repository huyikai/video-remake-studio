from __future__ import annotations

from typing import Any

from vrs.settings import Settings

GENERATE_PATHS: tuple[str, ...] = ("t2va", "t2va_turbo", "i2va_turbo", "ref2va")

# 早期把 I2VA 工作流错标成了 fl2va_turbo，旧 job.json 里还留着这个名字
PATH_ALIASES: dict[str, str] = {"fl2va_turbo": "i2va_turbo"}

# 走哪条路决定提示词首行指令，也决定要不要抽关键帧
PATH_KEYFRAMES: dict[str, int] = {"t2va": 0, "t2va_turbo": 0, "i2va_turbo": 1, "ref2va": 0}

# 外观锁按故事组（cast_reset 边界）向后传递：同一故事内逐字复用，跨故事重置。
# T2VA 文本锁锁不死脸（无图锚定），但服装/发型/道具/人数能锁住 —— 成片多故事拼接时必需。
PATH_LOCK_ACROSS: dict[str, bool] = {"t2va": True, "t2va_turbo": True, "i2va_turbo": True, "ref2va": True}

T2VA_WORKFLOWS: tuple[str, ...] = (
    "video_minimax_h3_t2v_turbo.json",
    "video_minimax_h3_t2v.json",
)
T2VA_DRAFT_DEFAULT: dict[str, Any] = {
    "workflow": "video_minimax_h3_t2v_turbo.json",
    "megapixels": 0.4,
    "steps": 8,
}
T2VA_FINAL_DEFAULT: dict[str, Any] = {
    "workflow": "video_minimax_h3_t2v.json",
    "megapixels": 0.98,
    "steps": 25,
}


def normalize_generate_path(name: str) -> str:
    path = PATH_ALIASES.get(str(name), str(name))
    if path not in GENERATE_PATHS:
        raise ValueError(f"未知的生成路线 {name!r}，可选 {', '.join(GENERATE_PATHS)}")
    return path


def _t2va_quality(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    workflow = str(raw.get("workflow") or fallback["workflow"])
    if workflow not in T2VA_WORKFLOWS:
        raise ValueError("T2VA 工作流只能是 T2VA Turbo 或 T2VA 非 LoRA")
    megapixels = float(raw["megapixels"] if raw.get("megapixels") is not None else fallback["megapixels"])
    steps = int(raw["steps"] if raw.get("steps") is not None else fallback["steps"])
    if megapixels <= 0 or steps < 1:
        raise ValueError("MP 和步数必须为正")
    return {"workflow": workflow, "megapixels": megapixels, "steps": steps}


def t2va_defaults(settings: Settings) -> dict[str, dict[str, Any]]:
    block = dict(settings.h3.get("t2va") or {})
    draft_fb, final_fb = dict(T2VA_DRAFT_DEFAULT), dict(T2VA_FINAL_DEFAULT)
    try:
        draft = _t2va_quality(dict(block.get("draft") or {}), draft_fb)
    except ValueError:
        draft = dict(draft_fb)
    try:
        final = _t2va_quality(dict(block.get("final") or {}), final_fb)
    except ValueError:
        final = dict(final_fb)
    return {"draft": draft, "final": final}


def merge_t2va_snapshot(settings: Settings, overlay: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    base = t2va_defaults(settings)
    if not overlay:
        return base
    out = {"draft": dict(base["draft"]), "final": dict(base["final"])}
    for key in ("draft", "final"):
        rec = overlay.get(key)
        if not isinstance(rec, dict):
            continue
        merged = dict(out[key])
        for field in ("workflow", "megapixels", "steps"):
            if rec.get(field) is not None:
                merged[field] = rec[field]
        out[key] = _t2va_quality(merged, out[key])
    return out


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
