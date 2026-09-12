"""用于 UI 开发的完整 Mock 流水线。

Mock 保留真实任务目录、阶段状态和文件协议，但把所有模型与 ComfyUI 调用替换为
确定性的本地数据。它只依赖标准库和可选的 ffmpeg，不读取用户输入的视频内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from vrs.ass import write_ass
from vrs.cancel import clear_cancel, raise_if_cancelled
from vrs.cover import write_cover
from vrs.deliver import trim_and_concat
from vrs.h3grid import PATH_KEYFRAMES, normalize_generate_path
from vrs.jobstore import STAGES, create_job, get_job, iter_jobs, mark_stage, save_status
from vrs.lock import JobLock, atomic_write_json, utcnow
from vrs.media import burn_ass, cut_clip, extract_frame_at
from vrs.passb import assemble_md, assemble_txt
from vrs.probe import ProbeError, probe_video
from vrs.settings import Settings
from vrs.worker import spawn


FIXTURE_RELATIVE = Path("data") / "mock" / "fixture.mp4"
SPEEDS = {"0.25x": 0.25, "1x": 1.0, "4x": 4.0}
STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}
MOCK_SOURCE_SECONDS = 30.0
MOCK_CLIP_SPAN = 5.8
MOCK_H3_SECONDS = 5.875
MOCK_H3_FRAMES = 141
MOCK_SPEECH = [
    {"t0": 0.8, "t1": 3.2, "text": "先看时间码，这是第一段。"},
    {"t0": 6.6, "t1": 9.0, "text": "第二段接着往前，画面还在动。"},
    {"t0": 12.4, "t1": 14.8, "text": "第三段过中点，继续往下走。"},
    {"t0": 18.2, "t1": 20.6, "text": "第四段节奏稳住，对白还在。"},
    {"t0": 24.0, "t1": 26.8, "text": "第五段收束，整片马上拼起来。"},
]
MOCK_EVENT_SUMMARIES = [
    "A quiet studio field with a running timecode in the corner.",
    "The muted panel drifts slowly while the second span plays.",
    "The same calm studio continues through the midpoint.",
    "The fourth span holds a steady, low-contrast composition.",
    "The closing span settles before the concat and burn.",
]


class MockError(RuntimeError):
    pass


def _clip_table() -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for index in range(len(MOCK_SPEECH)):
        t0 = round(index * MOCK_CLIP_SPAN, 3)
        t1 = round(t0 + MOCK_CLIP_SPAN, 3)
        clips.append(
            {
                "id": f"h3_{index + 1:02d}",
                "event_id": f"event-{index + 1:02d}",
                "t0": t0,
                "t1": t1,
                "source_seconds": MOCK_CLIP_SPAN,
                "h3_seconds": MOCK_H3_SECONDS,
                "h3_frames": MOCK_H3_FRAMES,
                "drift": round(MOCK_H3_SECONDS - MOCK_CLIP_SPAN, 3),
                "padded": True,
                "cast_reset": False,
            }
        )
    return clips


def _source_duration(job: dict[str, Any]) -> float:
    return float((job.get("source", {}).get("probe") or {}).get("duration") or MOCK_SOURCE_SECONDS)


def _mock_font() -> Path | None:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("msyh.ttc", "segoeui.ttf", "arial.ttf", "calibri.ttf", "consola.ttf"):
        path = fonts / name
        if path.is_file():
            return path
    return None


def _mock_video_filter() -> str:
    parts = [
        "format=yuv420p",
        "drawbox=x=0:y=0:w=iw:h=ih:color=0x1a2230@1:t=fill",
        "drawbox=x=72:y=72:w=iw-144:h=ih-168:color=0x2a384c@1:t=4",
        "drawbox=x='150+55*sin(2*PI*t/18)':y=250:w=380:h=150:color=0x4a5d70@0.35:t=fill",
        "drawbox=x=0:y=ih-72:w=iw:h=72:color=0x121820@0.85:t=fill",
    ]
    font = _mock_font()
    if font is not None:
        fontfile = str(font).replace("\\", "/").replace(":", r"\:")
        parts.append(
            f"drawtext=fontfile='{fontfile}':text='%{{pts\\:hms}}':fontsize=32:"
            "fontcolor=0xd7dee8:x=88:y=92:box=1:boxcolor=0x121820@0.35:boxborderw=8"
        )
    return ",".join(parts)


def _render_mock_source(dest: Path, seconds: float = MOCK_SOURCE_SECONDS) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MockError("Mock 需要 ffmpeg 才能生成带时间码的源片")
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x1a2230:s=1280x720:r=24:d={seconds:.3f}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=48000:cl=stereo:d={seconds:.3f}",
        "-vf",
        _mock_video_filter(),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not dest.is_file():
        raise MockError((completed.stderr or completed.stdout or "生成 Mock 源片失败").strip())
    probe = probe_video(dest)
    if float(probe["duration"]) < seconds - 0.5:
        raise MockError(f"Mock 源片时长 {probe['duration']:.2f}s，短于 {seconds:.0f}s")
    return probe


def _cut_mock_clip(source: Path, dest: Path, clip: dict[str, Any], *, log_path: Path | None) -> None:
    t0 = float(clip["t0"])
    t1 = float(clip["t1"])
    cut_clip(source, dest, t0, t1, log_path=log_path, keep_audio=True)
    probed = probe_video(dest)
    need = float(clip.get("source_seconds") or (t1 - t0))
    if float(probed["duration"]) < need - 0.3:
        raise MockError(f"{clip.get('id')} 切片时长 {probed['duration']:.2f}s，期望约 {need:.2f}s")


def _mock_prompt_hash(directory: Path, clip_id: str) -> str:
    """与 stages/generate.py 同算法（raw-bytes sha1 of prompts/{id}.txt）。"""
    path = directory / "prompts" / f"{clip_id}.txt"
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _fail(job: dict[str, Any], directory: Path, stage: str, message: str) -> None:
    _log(directory, message)
    mark_stage(job, directory, stage, "failed", error=message)
    job["state"] = "failed"
    job["note"] = message
    save_status(job, directory)


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "mock.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utcnow()} {text.rstrip()}\n")


def _load(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _speed(settings: Settings) -> float:
    return SPEEDS.get(settings.mock_speed(), 1.0)


def _wait(settings: Settings, seconds: float) -> None:
    time.sleep(max(0.05, float(seconds) / _speed(settings)))


def _trace(job: dict[str, Any], directory: Path, stage: str, model: str, input_: str, output: str, elapsed: float) -> None:
    items = list(job.get("model_trace") or [])
    items.append(
        {
            "stage": stage,
            "model": model,
            "input": input_,
            "output": output,
            "elapsed_sec": round(elapsed, 2),
            "mode": "mock",
            "at": utcnow(),
        }
    )
    job["model_trace"] = items[-20:]
    save_status(job, directory)


def _fault(job: dict[str, Any], directory: Path, stage: str, clip_id: str | None = None) -> bool:
    faults = job.get("options", {}).get("mock_faults") or {}
    config = faults.get(stage) if isinstance(faults, dict) else None
    if not isinstance(config, dict):
        return False
    target = str(config.get("clip_id") or "")
    if target and target != str(clip_id or ""):
        return False
    count = config.get("count", 1)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    consumed = job.setdefault("mock_faults_consumed", {})
    key = f"{stage}:{clip_id or '*'}"
    previous = int(consumed.get(key, 0) or 0)
    if count >= 0 and previous >= count:
        return False
    consumed[key] = previous + 1
    kind = str(config.get("type") or "simulated_error")
    message = f"Mock 模拟故障：{stage}{f'/{clip_id}' if clip_id else ''}，类型 {kind}"
    _log(directory, message)
    mark_stage(job, directory, stage, "failed", error=message)
    job["state"] = "failed"
    job["note"] = message + "。点击恢复后继续。"
    save_status(job, directory)
    return True


def _fixture(settings: Settings) -> Path | None:
    dest = settings.root / FIXTURE_RELATIVE
    if dest.is_file() and dest.stat().st_size > 1024:
        try:
            if float(probe_video(dest)["duration"]) >= MOCK_SOURCE_SECONDS - 0.5:
                return dest
        except (ProbeError, OSError, KeyError, TypeError, ValueError):
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_mock_source(dest)
    except MockError:
        return dest if dest.is_file() else None
    return dest


def _copy_fixture(settings: Settings, directory: Path) -> Path | None:
    source = _fixture(settings)
    if source is None:
        return None
    dest = directory / "source" / "video.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


def _mock_probe(video: Path | None) -> dict[str, Any]:
    if video is not None:
        try:
            return probe_video(video)
        except (ProbeError, OSError):
            pass
    return {
        "duration": MOCK_SOURCE_SECONDS,
        "width": 1280,
        "height": 720,
        "format": "mock",
        "size": video.stat().st_size if video and video.is_file() else 0,
        "path": str(video or "mock source"),
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def _set_source(job: dict[str, Any], directory: Path, settings: Settings) -> None:
    dest = directory / "source" / "video.mp4"
    probe = _render_mock_source(dest)
    source = job.setdefault("source", {})
    source["video"] = "source/video.mp4"
    source["probe"] = probe
    source["mock_fixture"] = "generated:studio"
    _write_json(
        directory / "source" / "ingest.json",
        {
            "kind": "mock",
            "input_kind": source.get("kind"),
            "input_url": source.get("url"),
            "input_path": source.get("original_path"),
            "fixture": "generated:studio",
            "duration": probe.get("duration"),
        },
    )
    job["note"] = "Mock 素材已准备，用户输入仅作为任务记录保存"
    save_status(job, directory)


def _write_understanding(job: dict[str, Any], directory: Path) -> None:
    clips = _clip_table()
    duration = _source_duration(job)
    shots = [{"t0": clip["t0"], "t1": clip["t1"]} for clip in clips]
    _write_json(directory / "shots.json", {"duration": duration, "shots": shots})
    _write_json(directory / "ocr" / "ocr.json", {"items": [], "engine": "mock:rapidocr"})
    full_text = "".join(item["text"] for item in MOCK_SPEECH)
    _write_json(
        directory / "transcript.full.json",
        {"engine": "mock:qwen3-asr", "model": "mock:qwen3-asr", "text": full_text, "segments": list(MOCK_SPEECH), "words": []},
    )
    _write_json(directory / "transcript.json", {"source": "mock", "segments": list(MOCK_SPEECH), "text": full_text})
    _write_json(directory / "dialogue.json", {"source": "mock", "speech": list(MOCK_SPEECH), "on_screen": []})
    windows = []
    events = []
    for clip, summary in zip(clips, MOCK_EVENT_SUMMARIES, strict=True):
        mid = round((float(clip["t0"]) + float(clip["t1"])) / 2, 3)
        windows.append(
            {
                "start": clip["t0"],
                "end": clip["t1"],
                "action": summary,
                "cells": [{"t": mid, "see": f"muted studio panel near {clip['id']}"}],
            }
        )
        events.append({"id": clip["event_id"], "kind": "story", "t0": clip["t0"], "t1": clip["t1"], "summary": summary})
    _write_json(directory / "beats.json", {"engine": "mock:qwen3-vl", "step": 0.25, "hop": 2.5, "windows": windows})
    _write_json(directory / "events.json", {"duration": duration, "events": events, "candidates": []})
    _write_json(
        directory / "understanding.json",
        {
            "done": True,
            "path": "mock",
            "duration": duration,
            "windows": len(windows),
            "events": len(events),
            "speech_segments": len(MOCK_SPEECH),
            "on_screen": 0,
            "vl_model": "mock:qwen3-vl",
            "llm_model": "mock:llm",
        },
    )
    _trace(job, directory, "understand", "mock:qwen3-asr + mock:qwen3-vl", "studio source", "shots, beats, events, dialogue", 0.9)


def _clips(job: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    path = normalize_generate_path(str(job.get("options", {}).get("generate_path") or "t2va_turbo"))
    clips = _clip_table()
    duration = _source_duration(job)
    if PATH_KEYFRAMES.get(path):
        for clip in clips:
            dest = directory / "keyframes" / f"{clip['id']}_a.jpg"
            video = directory / "source" / "video.mp4"
            if video.is_file():
                try:
                    extract_frame_at(video, dest, float(clip["t0"]), log_path=directory / "logs" / "mock.log")
                except Exception as exc:  # noqa: BLE001
                    _log(directory, f"关键帧降级：{exc}")
            clip["keyframe"] = f"keyframes/{dest.name}"
    _write_json(directory / "clips.all.json", {"duration": duration, "generate_path": path, "clips": clips})
    _write_json(directory / "clips.json", {"duration": duration, "t_min": 4.458, "t_max": 14.375, "generate_path": path, "only_clips": [], "clips": clips})
    prompts: list[dict[str, Any]] = []
    prompt_dir = directory / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    speaker = {
        "id": "S1",
        "lock": "Identity lock: an adult with a consistent face, hair, and layered clothing.",
        "voice": "Voice lock: adult Chinese speech, mid pitch, even rate.",
        "zh": "说话人",
    }
    for clip, line in zip(clips, MOCK_SPEECH, strict=True):
        clip_id = str(clip["id"])
        said = str(line["text"])
        doc = {
            "clip_id": clip_id,
            "generate_path": path,
            "style": "live-action photorealistic",
            "scene_lock": "Scene lock: a clean studio with controlled soft light, visible timecode, and a steady camera.",
            "speakers": [speaker],
            "shots": [
                {
                    "index": 1,
                    "at": None,
                    "text": (
                        "Identity lock: an adult with a consistent face, hair, and layered clothing. "
                        "Voice lock: adult Chinese speech, mid pitch, even rate. "
                        "Scene lock: preserve the clean studio and visible timecode. "
                        f"(S1) says <d>[Chinese] {said}</d> then holds still."
                    ),
                }
            ],
            "overall_soundscape": "A quiet studio room tone with spoken Chinese dialogue.",
            "non_diegetic_music": "N/A",
            "zh": {
                "event_chain": "时间码出现→对白→构图收束",
                "beats": ["动作保持连续，时间码随片段推进"],
                "amplitude": "中等；动作连续；结尾稳定",
                "note": "Mock 结果，用于 UI 开发",
            },
        }
        txt = assemble_txt(doc, clip, path)
        facts = {
            "speech": [
                {
                    "a": round(float(line["t0"]) - float(clip["t0"]), 3),
                    "b": round(float(line["t1"]) - float(clip["t0"]), 3),
                    "text": said,
                    "emotion": "",
                }
            ],
            "cuts": [],
            "vision": [],
            "actions": [],
            "adults": 1,
            "children": 0,
            "frames": [],
            "neighbors": [],
        }
        (prompt_dir / f"{clip_id}.txt").write_text(txt, encoding="utf-8")
        (prompt_dir / f"{clip_id}.md").write_text(assemble_md(doc, clip, facts, txt), encoding="utf-8")
        _write_json(prompt_dir / f"{clip_id}.json", doc)
        prompts.append(
            {
                "clip_id": clip_id,
                "generate_path": path,
                "h3_seconds": clip["h3_seconds"],
                "prompt": f"prompts/{clip_id}.txt",
                "review": f"prompts/{clip_id}.md",
                "speakers": ["S1"],
                "shots": [{"index": 1, "at": None}],
                "cast_reset": False,
            }
        )
    _write_json(directory / "prompts.json", {"generate_path": path, "negative_prompt": "", "prompts": prompts})
    _trace(job, directory, "script", "mock:llm", "events, beats, dialogue", f"{len(prompts)} H3 prompts", 0.7)
    return clips


def _review_mode(job: dict[str, Any]) -> str:
    return str((job.get("options") or {}).get("review_mode") or "pause_draft")


def _stage(job: dict[str, Any], directory: Path, settings: Settings, name: str, label: str) -> bool:
    started = time.monotonic()
    mark_stage(job, directory, name, "running")
    job["note"] = f"Mock 正在{label}"
    save_status(job, directory)
    _log(directory, f"{name} running")
    _wait(settings, 0.55)
    if _fault(job, directory, name):
        return False
    elapsed = time.monotonic() - started
    _trace(job, directory, name, f"mock:{name}", "fixture and task metadata", f"{label} 完成", elapsed)
    return True


def _run_prepare(settings: Settings, job: dict[str, Any], directory: Path, start: str = "download") -> bool:
    start_index = STAGE_INDEX.get(start, 0)
    if start_index <= STAGE_INDEX["download"] and job["stages"]["download"]["status"] != "done":
        if not _stage(job, directory, settings, "download", "准备素材"):
            return False
        _set_source(job, directory, settings)
        mark_stage(job, directory, "download", "done")
    if start_index <= STAGE_INDEX["pagemeta"] and job["stages"]["pagemeta"]["status"] not in {"done", "skipped"}:
        _write_json(directory / "source" / "page_meta.skipped.json", {"skipped": True, "reason": "Mock 不访问 URL"})
        mark_stage(job, directory, "pagemeta", "skipped", error="Mock 不访问网络")
    if start_index <= STAGE_INDEX["understand"] and job["stages"]["understand"]["status"] != "done":
        if not _stage(job, directory, settings, "understand", "理解视频"):
            return False
        _write_understanding(job, directory)
        mark_stage(job, directory, "understand", "done")
    if start_index <= STAGE_INDEX["script"] and job["stages"]["script"]["status"] != "done":
        if not _stage(job, directory, settings, "script", "生成脚本"):
            return False
        _clips(job, directory)
        mark_stage(job, directory, "script", "done")
    if start_index <= STAGE_INDEX["precheck"] and job["stages"]["precheck"]["status"] != "done":
        if not _stage(job, directory, settings, "precheck", "执行预检"):
            return False
        clips = list((_load(directory / "clips.json") or {}).get("clips") or _clip_table())
        audit = {
            "ok": True,
            "generate_path": normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo")),
            "errors": [],
            "warnings": ["Mock 模式使用生成的时间码源片"],
            "clips": [
                {
                    "clip_id": str(clip["id"]),
                    "h3_seconds": float(clip.get("h3_seconds") or MOCK_H3_SECONDS),
                    "shots": 1,
                    "speakers": ["S1"],
                    "lines": [MOCK_SPEECH[index]["text"]] if index < len(MOCK_SPEECH) else [],
                    "prompt": f"prompts/{clip['id']}.txt",
                    "review": f"prompts/{clip['id']}.md",
                }
                for index, clip in enumerate(clips)
            ],
        }
        _write_json(directory / "precheck.json", audit)
        (directory / "precheck.md").write_text("# Mock 预检报告\n\n结果：通过\n\nMock 模式使用生成的时间码源片。\n", encoding="utf-8")
        mark_stage(job, directory, "precheck", "done")
        job["stage"] = "generate"
        if _review_mode(job) == "full_auto":
            job["state"] = "running"
            job["note"] = "Mock 预检通过；自动试片"
        else:
            job["state"] = "paused"
            job["note"] = "Mock 预检通过；对照 prompts/*.md，审完后出试片"
        save_status(job, directory)
    return True


def _quality_ready(directory: Path, quality: str) -> bool:
    progress = _load(directory / "generate.json") or {}
    clips = _load(directory / "clips.json") or {}
    return bool(clips.get("clips")) and all((((progress.get("clips") or {}).get(str(clip["id"])) or {}).get(quality) or {}).get("status") == "done" for clip in clips.get("clips") or [])


def _generate(settings: Settings, job: dict[str, Any], directory: Path, quality: str, clip_ids: list[str] | None = None, *, chain: bool = True) -> bool:
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    wanted = {str(item) for item in clip_ids} if clip_ids else None
    path = normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo"))
    mark_stage(job, directory, "generate", "running")
    progress = _load(directory / "generate.json") or {"generate_path": path, "clips": {}, "events": []}
    progress["generate_path"] = path
    dest_dir = directory / "generate" / path / quality
    dest_dir.mkdir(parents=True, exist_ok=True)
    source = directory / "source" / "video.mp4"
    log_path = directory / "logs" / "mock.log"
    if not source.is_file():
        _fail(job, directory, "generate", "Mock 源片不存在，无法切片")
        return False
    made = 0
    for clip in clips:
        raise_if_cancelled(directory)
        clip_id = str(clip["id"])
        dest = dest_dir / f"{clip_id}.mp4"
        rec = (progress.setdefault("clips", {}).setdefault(clip_id, {})).setdefault(quality, {})
        if wanted is not None and clip_id not in wanted:
            continue
        if dest.is_file() and wanted is None:
            rec.update(
                {
                    "status": "done",
                    "file": f"generate/{path}/{quality}/{clip_id}.mp4",
                    "prompt_hash": _mock_prompt_hash(directory, clip_id),
                }
            )
            continue
        if _fault(job, directory, "generate", clip_id):
            return False
        rec.update({"status": "running", "attempts": 1})
        progress["last_clip"] = clip_id
        progress["last_quality"] = quality
        _write_json(directory / "generate.json", progress)
        _log(directory, f"{clip_id} {quality} running model=mock:minimax-h3")
        _wait(settings, 0.8)
        try:
            _cut_mock_clip(source, dest, clip, log_path=log_path)
        except Exception as exc:  # noqa: BLE001
            _fail(job, directory, "generate", f"Mock 切片失败：{exc}")
            return False
        rec.update(
            {
                "status": "done",
                "file": f"generate/{path}/{quality}/{clip_id}.mp4",
                "prompt_id": f"mock-{job['id']}-{clip_id}-{quality}",
                "prompt_hash": _mock_prompt_hash(directory, clip_id),
                "attempts": 1,
            }
        )
        progress.setdefault("events", []).append({"at": utcnow(), "kind": "clip_done", "clip_id": clip_id, "quality": quality, "model": "mock:minimax-h3"})
        _write_json(directory / "generate.json", progress)
        made += 1
    have = [clip for clip in clips if (dest_dir / f"{clip['id']}.mp4").is_file()]
    output = directory / "output" / path / f"{quality}.mp4"
    if have:
        try:
            trim_and_concat(have, src_dir=dest_dir, dest=output, work_dir=dest_dir / "trimmed", log_path=log_path)
        except Exception as exc:  # noqa: BLE001
            _fail(job, directory, "generate", f"Mock 合片失败：{exc}")
            return False
    progress[quality] = {"status": "done", "concat": f"output/{path}/{quality}.mp4", "clips": len(have)}
    _write_json(directory / "generate.json", progress)
    mark_stage(job, directory, "generate", "done")
    label = "试片" if quality == "draft" else "各段成片"
    count = len(wanted) if wanted else made or len(have)
    if not chain:
        job["state"] = "paused"
        job["stage"] = "generate"
        job["note"] = f"Mock {label}已更新 {count} 段" + ("，可拼接成片" if quality == "final" else "")
        save_status(job, directory)
        return True
    mode = _review_mode(job)
    if quality == "draft":
        job["stage"] = "generate"
        if mode == "full_auto":
            job["state"] = "running"
            job["note"] = f"Mock 试片已完成 {len(clips)} 段，自动出各段成片"
        else:
            job["state"] = "paused"
            job["note"] = f"Mock 试片已完成 {len(clips)} 段，确认后生成各段成片"
    elif mode == "full_auto":
        job["state"] = "running"
        job["stage"] = "finish"
        job["note"] = f"Mock 各段成片已完成 {len(clips)} 段，拼接成片"
    else:
        job["state"] = "paused"
        job["stage"] = "generate"
        job["note"] = f"Mock 各段成片已完成 {len(clips)} 段，确认后拼接成片"
    save_status(job, directory)
    return True


def _finish(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    path = normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo"))
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    source = directory / "generate" / path / "final"
    output_dir = directory / "output" / path
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = output_dir / "final.raw.mp4"
    final = output_dir / "final.mp4"
    ass_events = 0
    burned_ass = False
    expected = len((_load(directory / "dialogue.json") or {}).get("speech") or [])
    try:
        if not clips:
            raise MockError("缺少 clips.json，无法拼接")
        if not shutil.which("ffmpeg"):
            raise MockError("Mock 拼接成片需要 ffmpeg")
        trim_and_concat(clips, src_dir=source, dest=raw, work_dir=source / "trimmed-final", log_path=directory / "logs" / "mock.log")
        if bool(settings.default.get("ass_burn", True)):
            ass = output_dir / "final.ass"
            probe = probe_video(raw)
            play_res = (int(probe.get("width") or 1280), int(probe.get("height") or 720))
            ass_events = write_ass(
                ass,
                _load(directory / "dialogue.json") or {},
                clips,
                default_region=str(settings.default.get("ass_default_region") or "bottom"),
                play_res=play_res,
            )
            if expected and ass_events < expected:
                raise MockError(f"烧字条数不足：{ass_events}/{expected}")
            burn_ass(raw, ass, final, log_path=directory / "logs" / "mock.log")
            burned_ass = True
        else:
            shutil.copy2(raw, final)
        _write_json(
            directory / "finish.json",
            {
                "ok": True,
                "quality": "final",
                "clips": len(clips),
                "concat": True,
                "ass_burn": burned_ass,
                "ass_events": ass_events,
                "cover": None,
                "file": f"output/{path}/final.mp4",
            },
        )
    except Exception as exc:  # noqa: BLE001
        _write_json(
            directory / "finish.json",
            {"ok": False, "error": str(exc), "clips": len(clips), "ass_events": ass_events, "ass_burn": False},
        )
        _fail(job, directory, "finish", f"Mock 拼接成片失败：{exc}")
        return job
    cover = output_dir / "cover.jpg"
    if final.is_file() and shutil.which("ffmpeg"):
        try:
            write_cover(settings, cover, video=final, title="Mock Video Remake", log=lambda text: _log(directory, text))
        except Exception as exc:  # noqa: BLE001
            _log(directory, f"封面降级：{exc}")
    mark_stage(job, directory, "finish", "done")
    job["state"] = "done"
    job["stage"] = "finish"
    ass_note = f"，烧字 {ass_events} 条" if burned_ass else "，未烧字"
    job["note"] = f"Mock 拼接成片完成（{len(clips)} 段{ass_note}）"
    save_status(job, directory)
    return job


def _run(settings: Settings, job_id: str, start: str = "download") -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    lock = JobLock(settings)
    with lock.hold(job_id):
        clear_cancel(directory)
        try:
            mode = _review_mode(job)
            if start in {"download", "pagemeta", "understand", "script", "precheck", "generate"}:
                if not _run_prepare(settings, job, directory, start):
                    return job
            if start == "finish":
                return _finish(settings, job, directory)
            if start == "final":
                if not _generate(settings, job, directory, "final"):
                    return job
                if mode == "full_auto":
                    return _finish(settings, job, directory)
                return job
            if start == "generate":
                if not _generate(settings, job, directory, "draft"):
                    return job
                if mode != "full_auto":
                    return job
            if mode == "full_auto" and job.get("stages", {}).get("precheck", {}).get("status") == "done":
                if start != "generate" and not _quality_ready(directory, "draft"):
                    if not _generate(settings, job, directory, "draft"):
                        return job
                if not _quality_ready(directory, "final"):
                    if not _generate(settings, job, directory, "final"):
                        return job
                return _finish(settings, job, directory)
            return job
        except Exception as exc:  # noqa: BLE001
            mark_stage(job, directory, str(job.get("stage") or start), "failed", error=str(exc))
            job["state"] = "failed"
            job["note"] = f"Mock 执行失败：{exc}"
            save_status(job, directory)
            return job


def start_created(settings: Settings, job_id: str) -> None:
    spawn(settings, job_id, lambda: _run(settings, job_id, "download"))


def resume_now(settings: Settings, job_id: str) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    if job.get("stages", {}).get("finish", {}).get("status") == "done":
        return job
    if _review_mode(job) == "full_auto":
        return _run(settings, job_id, "download")
    if _quality_ready(directory, "final"):
        return _run(settings, job_id, "finish")
    if _quality_ready(directory, "draft"):
        return _run(settings, job_id, "final")
    job = _run(settings, job_id, "download")
    if job.get("stages", {}).get("precheck", {}).get("status") != "done":
        return job
    return _run(settings, job_id, "generate")


def draft_now(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    path = normalize_generate_path(str(job.get("options", {}).get("generate_path") or "t2va_turbo"))
    wanted = [str(item) for item in (clip_ids or [str(c["id"]) for c in ((_load(directory / "clips.json") or {}).get("clips") or [])])]
    for clip_id in wanted:
        (directory / "generate" / path / "draft" / f"{clip_id}.mp4").unlink(missing_ok=True)
        (directory / "generate" / path / "final" / f"{clip_id}.mp4").unlink(missing_ok=True)
    (directory / "output" / path / "draft.mp4").unlink(missing_ok=True)
    (directory / "output" / path / "final.mp4").unlink(missing_ok=True)
    if str(job.get("state") or "").lower() == "done":
        job["state"] = "paused"
        job["note"] = "脚本已改，正在重新出试片"
        save_status(job, directory)
    _generate(settings, job, directory, "draft", wanted, chain=False)
    _, job = get_job(settings, job_id)
    return job


def final_now(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    path = normalize_generate_path(str(job.get("options", {}).get("generate_path") or "t2va_turbo"))
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    dest_dir = directory / "generate" / path / "final"
    wanted = [str(item) for item in clip_ids] if clip_ids else [str(clip["id"]) for clip in clips if not (dest_dir / f"{clip['id']}.mp4").is_file()]
    for clip_id in wanted:
        (dest_dir / f"{clip_id}.mp4").unlink(missing_ok=True)
    (directory / "output" / path / "final.mp4").unlink(missing_ok=True)
    if str(job.get("state") or "").lower() == "done":
        job["state"] = "paused"
        job["note"] = "正在更新各段成片"
        save_status(job, directory)
    _generate(settings, job, directory, "final", wanted or None, chain=False)
    _, job = get_job(settings, job_id)
    return job


def assemble_now(settings: Settings, job_id: str) -> dict[str, Any]:
    return _run(settings, job_id, "finish")


def resume(settings: Settings, job_id: str) -> None:
    spawn(settings, job_id, lambda: resume_now(settings, job_id))


def draft(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> None:
    spawn(settings, job_id, lambda: draft_now(settings, job_id, clip_ids))


def final(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> None:
    spawn(settings, job_id, lambda: final_now(settings, job_id, clip_ids))


def assemble(settings: Settings, job_id: str) -> None:
    spawn(settings, job_id, lambda: assemble_now(settings, job_id))


def reset(settings: Settings) -> int:
    removed = 0
    for job in iter_jobs(settings):
        if str((job.get("options") or {}).get("mode")) != "mock":
            continue
        try:
            directory, _ = get_job(settings, str(job["id"]))
        except FileNotFoundError:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
    ensure_seed(settings)
    return removed


def _seed_job(settings: Settings, label: str, state: str) -> None:
    job = create_job(settings, kind="url", url=f"mock://{label}", review_mode="pause_draft", generate_path="t2va_turbo", aspect_confirmed=True)
    directory, job = get_job(settings, job["id"])
    job["options"]["seeded"] = True
    job["options"]["mode"] = "mock"
    _set_source(job, directory, settings)
    _write_understanding(job, directory)
    _clips(job, directory)
    for stage in ("download", "pagemeta", "understand", "script", "precheck"):
        mark_stage(job, directory, stage, "skipped" if stage == "pagemeta" else "done")
    if state == "waiting":
        mark_stage(job, directory, "understand", "waiting", error="等待 Mock 视觉分析")
        job["state"], job["stage"], job["note"] = "paused", "understand", "等待 Mock 视觉分析"
    elif state == "script":
        job["state"], job["stage"], job["note"] = "paused", "precheck", "脚本已生成，等待预检和审阅"
    elif state == "draft":
        _generate_seed_media(settings, job, directory, "draft")
        mark_stage(job, directory, "generate", "done")
        job["state"], job["stage"], job["note"] = "paused", "generate", "试片已完成，确认后生成各段成片"
    elif state == "done":
        _generate_seed_media(settings, job, directory, "draft")
        _generate_seed_media(settings, job, directory, "final")
        mark_stage(job, directory, "generate", "done")
        _finish(settings, job, directory)
        return
    elif state == "failed":
        mark_stage(job, directory, "generate", "failed", error="Mock 示例故障：Comfy 连接超时")
        job["state"], job["stage"], job["note"] = "failed", "generate", "Mock 示例故障：Comfy 连接超时。点击恢复重试。"
    else:
        job["state"], job["stage"], job["note"] = "paused", "precheck", "等待开始 Mock 生成"
    save_status(job, directory)


def _generate_seed_media(settings: Settings, job: dict[str, Any], directory: Path, quality: str) -> None:
    path = normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo"))
    dest = directory / "generate" / path / quality
    dest.mkdir(parents=True, exist_ok=True)
    source = directory / "source" / "video.mp4"
    if not source.is_file():
        _render_mock_source(source)
        job.setdefault("source", {})["probe"] = probe_video(source)
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    log_path = directory / "logs" / "mock.log"
    for clip in clips:
        _cut_mock_clip(source, dest / f"{clip['id']}.mp4", clip, log_path=log_path)
    output = directory / "output" / path / f"{quality}.mp4"
    trim_and_concat(clips, src_dir=dest, dest=output, work_dir=dest / "trimmed-seed", log_path=log_path)
    progress = _load(directory / "generate.json") or {"generate_path": path, "clips": {}}
    for clip in clips:
        cid = str(clip["id"])
        progress.setdefault("clips", {}).setdefault(cid, {})[quality] = {
            "status": "done",
            "file": f"generate/{path}/{quality}/{cid}.mp4",
            "attempts": 1,
            "prompt_id": f"mock-seed-{quality}-{cid}",
            "prompt_hash": _mock_prompt_hash(directory, cid),
        }
    progress[quality] = {"status": "done", "concat": f"output/{path}/{quality}.mp4", "clips": len(clips)}
    _write_json(directory / "generate.json", progress)


def ensure_seed(settings: Settings) -> None:
    if settings.mode() != "mock":
        return
    if any(str((job.get("options") or {}).get("mode")) == "mock" for job in iter_jobs(settings)):
        return
    for label, state in (("waiting", "waiting"), ("script-review", "script"), ("draft-review", "draft"), ("rendering", "rendering"), ("failed", "failed"), ("complete", "done")):
        _seed_job(settings, label, state)


def create_mock_job(settings: Settings, **kwargs: Any) -> dict[str, Any]:
    kwargs.pop("background", None)
    job = create_job(settings, **kwargs)
    start_created(settings, job["id"])
    return job
