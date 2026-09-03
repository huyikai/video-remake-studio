from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vrs.h3grid import PATH_KEYFRAMES, PATH_LOCK_ACROSS, normalize_generate_path, t_bounds
from vrs.jobstore import mark_stage, only_clips, save_status
from vrs.lock import atomic_write_json
from vrs.media import extract_frame_at
from vrs.packing import pack_h3_clips
from vrs.passb import PassBError, write_prompts
from vrs.promptcheck import check_locks
from vrs.settings import Settings


class ScriptError(RuntimeError):
    pass


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "script.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pack(settings: Settings, events_doc: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    events = list(events_doc.get("events") or [])
    _t_min, t_max = t_bounds(settings)
    clips = pack_h3_clips(
        events,
        settings=settings,
        candidates=list(events_doc.get("candidates") or []),
    )
    if not clips:
        raise ScriptError("打包后没有故事段（整片都被标成了片尾字卡？）")
    over = [c for c in clips if float(c["source_seconds"]) > t_max + 1e-6]
    if over:
        raise ScriptError(f"有 {len(over)} 段仍超过 H3 上限 {t_max:.3f}s，拆分失败")
    n_split = sum(1 for c in clips if c.get("split_from"))
    n_pad = sum(1 for c in clips if c.get("padded"))
    worst = max((abs(float(c["drift"])) for c in clips), default=0.0)
    n_skip = sum(1 for e in events if str(e.get("kind") or "") == "endcard")
    _log(
        directory,
        f"打包 clips={len(clips)} 事件={len(events)} 跳过片尾={n_skip} 二次拆分={n_split} "
        f"补时长={n_pad} 最大网格漂移={worst:.2f}s",
    )
    return clips


def _keyframes(clips: list[dict[str, Any]], job: dict[str, Any], directory: Path) -> int:
    """I2VA 路要给每段抽首帧。首帧就是源片区间的起点。"""
    video = directory / str((job.get("source") or {}).get("video") or "source/video.mp4")
    if not video.is_file():
        raise ScriptError(f"找不到源视频 {video}")
    dest_dir = directory / "keyframes"
    dest_dir.mkdir(parents=True, exist_ok=True)
    made = 0
    for clip in clips:
        dest = dest_dir / f"{clip['id']}_a.jpg"
        if not dest.is_file():
            extract_frame_at(video, dest, float(clip["t0"]), log_path=directory / "logs" / "script.log")
            made += 1
        clip["keyframe"] = f"keyframes/{dest.name}"
    return made


def run_script(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    events_doc = _load_json(directory / "events.json")
    if not (events_doc or {}).get("events"):
        raise ScriptError("缺少 events.json，先跑完 understand")
    beats = _load_json(directory / "beats.json")
    if not (beats or {}).get("windows"):
        raise ScriptError("缺少 beats.json，先跑完拍表")
    dialogue = _load_json(directory / "dialogue.json") or {}
    cuts = [float(c) for c in ((_load_json(directory / "scene_cuts.json") or {}).get("cuts") or [])]
    path = normalize_generate_path((job.get("options") or {}).get("generate_path") or "i2va_turbo")

    mark_stage(job, directory, "script", "running")
    try:
        assert events_doc is not None and beats is not None
        clips = _pack(settings, events_doc, directory)
        wanted = only_clips(job)
        atomic_write_json(
            directory / "clips.all.json",
            {
                "duration": float(events_doc.get("duration") or 0),
                "generate_path": path,
                "clips": clips,
            },
        )
        if wanted:
            have = {str(c["id"]) for c in clips}
            missing = [cid for cid in wanted if cid not in have]
            if missing:
                raise ScriptError(f"没有这些段：{', '.join(missing)}（打包后是 {', '.join(sorted(have))}）")
            keep = set(wanted)
            clips = [c for c in clips if str(c["id"]) in keep]
            _log(directory, f"只写 {len(clips)} 段做验证：{', '.join(wanted)}")
        if PATH_KEYFRAMES.get(path):
            made = _keyframes(clips, job, directory)
            _log(directory, f"抽首帧 {made} 张（{path}）")
        t_min, t_max = t_bounds(settings)
        atomic_write_json(
            directory / "clips.json",
            {
                "duration": float(events_doc.get("duration") or 0),
                "t_min": round(t_min, 3),
                "t_max": round(t_max, 3),
                "generate_path": path,
                "only_clips": wanted,
                "clips": clips,
            },
        )

        _log(directory, f"开始写英文提示词（{path}，共 {len(clips)} 段）")
        items = write_prompts(
            settings,
            clips,
            beats,
            dialogue,
            cuts,
            directory=directory,
            path=path,
            log=lambda text: _log(directory, text),
        )
        if PATH_LOCK_ACROSS.get(path, True):
            cross = check_locks(items)
            if cross:
                raise ScriptError("说话人外观锁跨段不一致：\n" + "\n".join(cross))
        atomic_write_json(
            directory / "prompts.json",
            {"generate_path": path, "negative_prompt": settings.h3.get("negative_prompt") or "", "prompts": items},
        )
        _log(directory, f"提示词写完 {len(items)} 段")

        mark_stage(job, directory, "script", "done")
        job["state"] = "paused"
        job["stage"] = "precheck"
        extra = f"，验证 {', '.join(wanted)}" if wanted else ""
        job["note"] = f"已写好 {len(items)} 段 H3 提示词（{path}{extra}）"
        save_status(job, directory)
        return job
    except (ScriptError, PassBError, ValueError, KeyError, TypeError) as exc:
        mark_stage(job, directory, "script", "failed", error=str(exc))
        raise
