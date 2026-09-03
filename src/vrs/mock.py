"""用于 UI 开发的完整 Mock 流水线。

Mock 保留真实任务目录、阶段状态和文件协议，但把所有模型与 ComfyUI 调用替换为
确定性的本地数据。它只依赖标准库和可选的 ffmpeg，不读取用户输入的视频内容。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

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


class MockError(RuntimeError):
    pass


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
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    sibling = settings.root.parent / "minmax-h3-studio" / "scripts" / "mock-output.mp4"
    if sibling.is_file():
        shutil.copy2(sibling, dest)
        return dest
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "12",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return dest if completed.returncode == 0 and dest.is_file() else None


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
        "duration": 12.0,
        "width": 640,
        "height": 360,
        "format": "mock",
        "size": video.stat().st_size if video and video.is_file() else 0,
        "path": str(video or "mock fixture"),
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def _set_source(job: dict[str, Any], directory: Path, settings: Settings) -> None:
    video = _copy_fixture(settings, directory)
    probe = _mock_probe(video)
    source = job.setdefault("source", {})
    source["video"] = "source/video.mp4"
    source["probe"] = probe
    source["mock_fixture"] = str(FIXTURE_RELATIVE)
    _write_json(
        directory / "source" / "ingest.json",
        {
            "kind": "mock",
            "input_kind": source.get("kind"),
            "input_url": source.get("url"),
            "input_path": source.get("original_path"),
            "fixture": str(FIXTURE_RELATIVE),
        },
    )
    job["note"] = "Mock 素材已准备，用户输入仅作为任务记录保存"
    save_status(job, directory)


def _write_understanding(job: dict[str, Any], directory: Path) -> None:
    duration = float((job.get("source", {}).get("probe") or {}).get("duration") or 12.0)
    _write_json(directory / "shots.json", {"duration": duration, "shots": [{"t0": 0.0, "t1": 5.8}, {"t0": 5.8, "t1": 11.6}]})
    _write_json(directory / "ocr" / "ocr.json", {"items": [], "engine": "mock:rapidocr"})
    _write_json(
        directory / "transcript.full.json",
        {"engine": "mock:qwen3-asr", "model": "mock:qwen3-asr", "text": "", "segments": [], "words": []},
    )
    _write_json(directory / "transcript.json", {"source": "mock", "segments": [], "text": ""})
    _write_json(directory / "dialogue.json", {"source": "mock", "speech": [], "on_screen": []})
    windows = [
        {"start": 0.0, "end": 3.0, "action": "A colorful test scene moves gently across the frame.", "cells": [{"t": 0.0, "see": "abstract color bars and a soft studio grid"}]},
        {"start": 2.5, "end": 5.8, "action": "The scene shifts to a brighter composition with a steady camera.", "cells": [{"t": 3.0, "see": "bright geometric shapes in a clean studio"}]},
        {"start": 5.8, "end": 9.0, "action": "The composition changes while the motion remains smooth.", "cells": [{"t": 6.0, "see": "a centered graphic field with warm highlights"}]},
        {"start": 8.5, "end": 11.6, "action": "The final movement settles into a clear closing frame.", "cells": [{"t": 9.0, "see": "a balanced abstract frame with a dark edge"}]},
    ]
    _write_json(directory / "beats.json", {"engine": "mock:qwen3-vl", "step": 0.25, "hop": 2.5, "windows": windows})
    _write_json(
        directory / "events.json",
        {
            "duration": duration,
            "events": [
                {"id": "event-01", "kind": "story", "t0": 0.0, "t1": 5.8, "summary": "The visual composition appears and moves through the first setup."},
                {"id": "event-02", "kind": "story", "t0": 5.8, "t1": 11.6, "summary": "The composition changes and settles into a closing beat."},
            ],
            "candidates": [],
        },
    )
    _write_json(directory / "understanding.json", {"done": True, "path": "mock", "duration": duration, "windows": 4, "events": 2, "speech_segments": 0, "on_screen": 0, "vl_model": "mock:qwen3-vl", "llm_model": "mock:llm"})
    _trace(job, directory, "understand", "mock:qwen3-asr + mock:qwen3-vl", "fixture video", "shots, beats, events, dialogue", 0.9)


def _clips(job: dict[str, Any], directory: Path) -> list[dict[str, Any]]:
    path = normalize_generate_path(str(job.get("options", {}).get("generate_path") or "t2va_turbo"))
    clips = [
        {"id": "h3_01", "event_id": "event-01", "t0": 0.0, "t1": 5.8, "source_seconds": 5.8, "h3_seconds": 5.875, "h3_frames": 141, "drift": 0.075, "padded": True, "cast_reset": False},
        {"id": "h3_02", "event_id": "event-02", "t0": 5.8, "t1": 11.6, "source_seconds": 5.8, "h3_seconds": 5.875, "h3_frames": 141, "drift": 0.075, "padded": True, "cast_reset": False},
    ]
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
    _write_json(directory / "clips.all.json", {"duration": 12.0, "generate_path": path, "clips": clips})
    _write_json(directory / "clips.json", {"duration": 12.0, "t_min": 4.458, "t_max": 14.375, "generate_path": path, "only_clips": [], "clips": clips})
    prompts: list[dict[str, Any]] = []
    prompt_dir = directory / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        clip_id = str(clip["id"])
        doc = {
            "clip_id": clip_id,
            "generate_path": path,
            "style": "live-action photorealistic",
            "scene_lock": "Scene lock: a clean studio with controlled soft light, neutral surfaces, and a steady camera.",
            "speakers": [],
            "shots": [{"index": 1, "at": None, "text": "Identity lock: no speaking characters, clear geometric subjects. Scene lock: preserve the clean studio. The camera moves slowly while the composition shifts from one balanced arrangement to another."}],
            "overall_soundscape": "A quiet studio room tone with a soft electronic movement and no spoken dialogue.",
            "non_diegetic_music": "N/A",
            "zh": {"event_chain": "画面出现→构图移动→稳定收束", "beats": ["动作保持连续，构图从开场向结尾平滑变化"], "amplitude": "中等；动作连续；结尾稳定", "note": "Mock 结果，用于 UI 开发"},
        }
        txt = assemble_txt(doc, clip, path)
        (prompt_dir / f"{clip_id}.txt").write_text(txt, encoding="utf-8")
        (prompt_dir / f"{clip_id}.md").write_text(assemble_md(doc, clip, {"speech": [], "cuts": [], "vision": [], "actions": [], "adults": 0, "children": 0, "frames": [], "neighbors": []}, txt), encoding="utf-8")
        _write_json(prompt_dir / f"{clip_id}.json", doc)
        prompts.append({"clip_id": clip_id, "generate_path": path, "h3_seconds": clip["h3_seconds"], "prompt": f"prompts/{clip_id}.txt", "review": f"prompts/{clip_id}.md", "speakers": [], "shots": [{"index": 1, "at": None}], "cast_reset": False})
    _write_json(directory / "prompts.json", {"generate_path": path, "negative_prompt": "", "prompts": prompts})
    _trace(job, directory, "script", "mock:llm", "events, beats, dialogue", f"{len(prompts)} H3 prompts", 0.7)
    return clips


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
        audit = {"ok": True, "generate_path": normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo")), "errors": [], "warnings": ["Mock 模式使用内置素材"], "clips": [{"clip_id": "h3_01", "h3_seconds": 5.875, "shots": 1, "speakers": [], "lines": [], "prompt": "prompts/h3_01.txt", "review": "prompts/h3_01.md"}, {"clip_id": "h3_02", "h3_seconds": 5.875, "shots": 1, "speakers": [], "lines": [], "prompt": "prompts/h3_02.txt", "review": "prompts/h3_02.md"}]}
        _write_json(directory / "precheck.json", audit)
        (directory / "precheck.md").write_text("# Mock 预检报告\n\n结果：通过\n\nMock 模式使用内置素材。\n", encoding="utf-8")
        mark_stage(job, directory, "precheck", "done")
    return True


def _quality_ready(directory: Path, quality: str) -> bool:
    progress = _load(directory / "generate.json") or {}
    clips = _load(directory / "clips.json") or {}
    return bool(clips.get("clips")) and all((((progress.get("clips") or {}).get(str(clip["id"])) or {}).get(quality) or {}).get("status") == "done" for clip in clips.get("clips") or [])


def _generate(settings: Settings, job: dict[str, Any], directory: Path, quality: str) -> bool:
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    path = normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo"))
    mark_stage(job, directory, "generate", "running")
    progress = _load(directory / "generate.json") or {"generate_path": path, "clips": {}, "events": []}
    progress["generate_path"] = path
    dest_dir = directory / "generate" / path / quality
    dest_dir.mkdir(parents=True, exist_ok=True)
    source = directory / "source" / "video.mp4"
    for clip in clips:
        raise_if_cancelled(directory)
        clip_id = str(clip["id"])
        if _fault(job, directory, "generate", clip_id):
            return False
        rec = (progress.setdefault("clips", {}).setdefault(clip_id, {})).setdefault(quality, {})
        dest = dest_dir / f"{clip_id}.mp4"
        rec.update({"status": "running", "attempts": 1})
        progress["last_clip"] = clip_id
        progress["last_quality"] = quality
        _write_json(directory / "generate.json", progress)
        _log(directory, f"{clip_id} {quality} running model=mock:minimax-h3")
        _wait(settings, 0.8)
        if source.is_file() and shutil.which("ffmpeg"):
            try:
                cut_clip(source, dest, float(clip["t0"]), float(clip["t1"]), log_path=directory / "logs" / "mock.log")
            except Exception as exc:  # noqa: BLE001
                _log(directory, f"切片失败，复制 fixture：{exc}")
                fixture = _fixture(settings)
                if fixture:
                    shutil.copy2(fixture, dest)
        else:
            fixture = _fixture(settings)
            if fixture:
                shutil.copy2(fixture, dest)
        rec.update({"status": "done", "file": f"generate/{path}/{quality}/{clip_id}.mp4", "prompt_id": f"mock-{job['id']}-{clip_id}-{quality}", "attempts": 1})
        progress.setdefault("events", []).append({"at": utcnow(), "kind": "clip_done", "clip_id": clip_id, "quality": quality, "model": "mock:minimax-h3"})
        _write_json(directory / "generate.json", progress)
    output = directory / "output" / path / f"{quality}.mp4"
    try:
        trim_and_concat(clips, src_dir=dest_dir, dest=output, work_dir=dest_dir / "trimmed", log_path=directory / "logs" / "mock.log")
    except Exception as exc:  # noqa: BLE001
        fixture = _fixture(settings)
        if fixture:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture, output)
        _log(directory, f"合片降级：{exc}")
    progress[quality] = {"status": "done", "concat": f"output/{path}/{quality}.mp4", "clips": len(clips)}
    _write_json(directory / "generate.json", progress)
    mark_stage(job, directory, "generate", "done")
    job["state"] = "paused"
    job["stage"] = "finish"
    job["note"] = f"Mock {quality} 已完成 {len(clips)} 段，等待下一步操作"
    save_status(job, directory)
    return True


def _finish(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    path = normalize_generate_path(str(job["options"].get("generate_path") or "t2va_turbo"))
    clips = list((_load(directory / "clips.json") or {}).get("clips") or [])
    source = directory / "generate" / path / "final"
    output_dir = directory / "output" / path
    raw = output_dir / "final.raw.mp4"
    final = output_dir / "final.mp4"
    try:
        trim_and_concat(clips, src_dir=source, dest=raw, work_dir=source / "trimmed-final", log_path=directory / "logs" / "mock.log")
        if bool(settings.default.get("ass_burn", True)) and shutil.which("ffmpeg"):
            ass = output_dir / "final.ass"
            from vrs.ass import write_ass

            write_ass(ass, _load(directory / "dialogue.json") or {}, clips, default_region="bottom")
            burn_ass(raw, ass, final, log_path=directory / "logs" / "mock.log")
        else:
            shutil.copy2(raw, final)
    except Exception as exc:  # noqa: BLE001
        fallback = output_dir / "final.mp4"
        draft = output_dir / "draft.mp4"
        if draft.is_file():
            shutil.copy2(draft, fallback)
        _log(directory, f"交付降级：{exc}")
    cover = output_dir / "cover.jpg"
    if final.is_file() and shutil.which("ffmpeg"):
        try:
            write_cover(settings, cover, video=final, title="Mock Video Remake", log=lambda text: _log(directory, text))
        except Exception as exc:  # noqa: BLE001
            _log(directory, f"封面降级：{exc}")
    mark_stage(job, directory, "finish", "done")
    job["state"] = "done"
    job["stage"] = "finish"
    job["note"] = "Mock 成片已完成，可播放和下载"
    save_status(job, directory)
    return job


def _run(settings: Settings, job_id: str, start: str = "download", auto: bool = False) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    lock = JobLock(settings)
    with lock.hold(job_id):
        clear_cancel(directory)
        try:
            if start in {"download", "pagemeta", "understand", "script", "precheck", "generate"}:
                if not _run_prepare(settings, job, directory, start):
                    return job
            if auto or start == "generate":
                if not _generate(settings, job, directory, "draft"):
                    return job
                if auto and str(job["options"].get("review_mode")) == "full_auto":
                    if not _generate(settings, job, directory, "final"):
                        return job
                    return _finish(settings, job, directory)
            elif start == "final":
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
    def run() -> None:
        job = _run(settings, job_id, "download")
        if job.get("stages", {}).get("precheck", {}).get("status") != "done":
            return
        if str(job.get("options", {}).get("review_mode")) != "full_auto":
            return
        if not _run(settings, job_id, "generate"):
            return
        next_job = get_job(settings, job_id)[1]
        if _run(settings, job_id, "final"):
            _finish(settings, next_job, get_job(settings, job_id)[0])

    spawn(settings, job_id, run)


def resume_now(settings: Settings, job_id: str) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    if _quality_ready(directory, "draft"):
        job["state"] = "paused"
        job["stage"] = "finish"
        job["note"] = "Mock 草稿已齐，点击出成片继续"
        save_status(job, directory)
        return job
    job = _run(settings, job_id, "download")
    if job.get("stages", {}).get("precheck", {}).get("status") != "done":
        return job
    return _run(settings, job_id, "generate")


def draft_now(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> dict[str, Any]:
    directory, job = get_job(settings, job_id)
    path = normalize_generate_path(str(job.get("options", {}).get("generate_path") or "t2va_turbo"))
    wanted = set(clip_ids or [str(c["id"]) for c in ((_load(directory / "clips.json") or {}).get("clips") or [])])
    for clip_id in wanted:
        (directory / "generate" / path / "draft" / f"{clip_id}.mp4").unlink(missing_ok=True)
    return _run(settings, job_id, "generate")


def final_now(settings: Settings, job_id: str) -> dict[str, Any]:
    return _run(settings, job_id, "final")


def resume(settings: Settings, job_id: str) -> None:
    spawn(settings, job_id, lambda: resume_now(settings, job_id))


def draft(settings: Settings, job_id: str, clip_ids: list[str] | None = None) -> None:
    spawn(settings, job_id, lambda: draft_now(settings, job_id, clip_ids))


def final(settings: Settings, job_id: str) -> None:
    spawn(settings, job_id, lambda: final_now(settings, job_id))


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
        job["state"], job["stage"], job["note"] = "paused", "finish", "草稿已完成，等待审片"
    elif state == "done":
        _generate_seed_media(settings, job, directory, "draft")
        _generate_seed_media(settings, job, directory, "final")
        mark_stage(job, directory, "generate", "done")
        mark_stage(job, directory, "finish", "done")
        job["state"], job["stage"], job["note"] = "done", "finish", "Mock 成片已完成"
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
    fixture = _fixture(settings)
    for clip in ((_load(directory / "clips.json") or {}).get("clips") or []):
        target = dest / f"{clip['id']}.mp4"
        if fixture:
            shutil.copy2(fixture, target)
    output = directory / "output" / path / f"{quality}.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    if fixture:
        shutil.copy2(fixture, output)
    progress = _load(directory / "generate.json") or {"generate_path": path, "clips": {}}
    for clip in ((_load(directory / "clips.json") or {}).get("clips") or []):
        progress.setdefault("clips", {}).setdefault(str(clip["id"]), {})[quality] = {"status": "done", "file": f"generate/{path}/{quality}/{clip['id']}.mp4", "attempts": 1, "prompt_id": f"mock-seed-{quality}-{clip['id']}"}
    progress[quality] = {"status": "done", "concat": f"output/{path}/{quality}.mp4", "clips": 2}
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
