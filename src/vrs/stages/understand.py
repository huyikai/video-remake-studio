from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vrs.asr import resolve_aligner_model, resolve_asr_model, transcribe_wav, transcript_stale
from vrs.beats import run_beat_table, window_failed
from vrs.cancel import JobCancelled, raise_if_cancelled
from vrs.dialogue import (
    adjudicate_ocr_asr,
    finalize_transcript,
    merge_dialogue,
    needs_ocr_asr_judge,
    ocr_bottom_lines,
    to_full_transcript,
)
from vrs.jobstore import mark_stage, save_status
from vrs.llmclient import LLMError, llm_health, llm_label, unload_llm
from vrs.lock import atomic_write_json
from vrs.media import extract_wav
from vrs.ocrframes import ensure_ocr_models, run_ocr
from vrs.passa import run_pass_a
from vrs.probe import ProbeError, probe_video
from vrs.ser import attach_vocal_emotion, resolve_ser_model
from vrs.settings import Settings
from vrs.shots import detect_shots
from vrs.vlclient import VLError, resolve_vl, unload_vl, vl_health

VIDEO_NAME = "video.mp4"


class UnderstandError(RuntimeError):
    pass


class UnderstandWaiting(RuntimeError):
    pass


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "understand.log"
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


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_json(path, data)


def _video_path(directory: Path) -> Path:
    return directory / "source" / VIDEO_NAME


def _speech_key(dialogue: dict[str, Any] | None) -> list[tuple[Any, ...]]:
    if not dialogue:
        return []
    return [
        (
            round(float(item.get("t0") or 0), 3),
            round(float(item.get("t1") or 0), 3),
            str(item.get("text") or ""),
            str(item.get("source") or ""),
        )
        for item in dialogue.get("speech") or []
    ]


def _reuse_ser(existing: dict[str, Any] | None, fresh: dict[str, Any]) -> bool:
    if not existing or not existing.get("speech"):
        return False
    if _speech_key(existing) != _speech_key(fresh):
        return False
    return all("vocal_emotion" in item for item in existing.get("speech") or [])


def _copy_emotions(transcript: dict[str, Any], dialogue: dict[str, Any]) -> None:
    by_span = {
        (round(float(item["t0"]), 3), round(float(item["t1"]), 3)): item.get("vocal_emotion")
        for item in dialogue.get("speech") or []
    }
    for seg in transcript.get("segments") or []:
        seg["vocal_emotion"] = by_span.get(
            (round(float(seg.get("start") or 0), 3), round(float(seg.get("end") or 0), 3))
        )


def _missing_local(settings: Settings) -> list[str]:
    reasons: list[str] = []
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        reasons.append("未安装 transformers/torch（uv sync --extra asr --extra vl）")
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        reasons.append("未安装 RapidOCR（uv sync --extra asr）")
    try:
        import scenedetect  # noqa: F401
    except ImportError:
        reasons.append("未安装 scenedetect（uv sync --extra asr）")
    try:
        resolve_asr_model(settings)
        resolve_aligner_model(settings)
        resolve_ser_model(settings)
    except RuntimeError as exc:
        reasons.append(str(exc))
    try:
        ensure_ocr_models(settings)
    except Exception as exc:  # noqa: BLE001
        reasons.append(str(exc))
    return reasons


def _asr_prompt(directory: Path, ocr: dict[str, Any] | None = None) -> str:
    terms: list[str] = []
    for line in ocr_bottom_lines(ocr):
        text = str(line or "").strip()
        if not (2 <= len(text) <= 14):
            continue
        if text in terms:
            continue
        terms.append(text)
        if len(terms) >= 10:
            break
    if not terms:
        return "简体中文口播。"
    return "Vocabulary: " + "，".join(terms)


def _events_done(directory: Path) -> bool:
    events = _load_json(directory / "events.json")
    beats = _load_json(directory / "beats.json")
    if not events or not isinstance(events.get("events"), list) or not events["events"]:
        return False
    windows = list((beats or {}).get("windows") or [])
    if not windows:
        return False
    if any(window_failed(window) for window in windows):
        return False
    return True


def _beats_complete(beats: dict[str, Any] | None, duration: float) -> bool:
    if not beats:
        return False
    windows = beats.get("windows") or []
    if not windows:
        return False
    errors = [w for w in windows if window_failed(w)]
    if errors:
        return False
    last = max(float(w.get("end") or 0) for w in windows)
    if last < duration - 0.6:
        return False
    hop = float(beats.get("hop") or 2.5)
    starts = sorted(float(w.get("start") or 0) for w in windows)
    return all(b - a <= hop + 0.1 for a, b in zip(starts, starts[1:]))


UNDERSTAND_STEPS: tuple[tuple[str, str], ...] = (
    ("shots", "镜头检测"),
    ("ocr", "画面文字"),
    ("asr", "语音转写"),
    ("dialogue", "口播与情绪"),
    ("beats", "画面拍表"),
    ("events", "事件归纳"),
)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def set_understand_progress(
    job: dict[str, Any],
    directory: Path,
    *,
    step: str,
    wait: str | None = None,
    window: int | None = None,
    windows: int | None = None,
    window_t0: float | None = None,
    window_t1: float | None = None,
    window_failed: int | None = None,
    detail: str | None = None,
    failed: bool = False,
) -> None:
    raise_if_cancelled(directory)
    labels = dict(UNDERSTAND_STEPS)
    if step not in labels:
        step = "shots"
    prev = job.get("understand_progress") if isinstance(job.get("understand_progress"), dict) else {}
    started = prev.get("step_started_at") if prev.get("step") == step else None
    if not started:
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    beat_window = _opt_int(window if window is not None else prev.get("window") if step == "beats" else None)
    beat_windows = _opt_int(windows if windows is not None else prev.get("windows") if step == "beats" else None)
    if step == "beats":
        t0 = _opt_float(window_t0 if window_t0 is not None else prev.get("window_t0"))
        t1 = _opt_float(window_t1 if window_t1 is not None else prev.get("window_t1"))
        nfail = _opt_int(window_failed if window_failed is not None else prev.get("window_failed"))
    else:
        t0 = t1 = nfail = None
        beat_window = beat_windows = None
    ids = [item[0] for item in UNDERSTAND_STEPS]
    idx = ids.index(step)
    steps: list[dict[str, Any]] = []
    for i, (sid, label) in enumerate(UNDERSTAND_STEPS):
        if i < idx:
            status = "done"
        elif i == idx:
            status = "failed" if failed else ("waiting" if wait else "active")
        else:
            status = "pending"
        rec: dict[str, Any] = {"id": sid, "label": label, "status": status}
        if sid == "beats" and beat_windows is not None:
            rec["window"] = int(beat_window or 0)
            rec["windows"] = int(beat_windows)
            if t0 is not None:
                rec["window_t0"] = t0
            if t1 is not None:
                rec["window_t1"] = t1
            if nfail is not None:
                rec["window_failed"] = nfail
        steps.append(rec)
    if failed:
        chip = labels[step]
        note = (detail or "").strip() or chip
    elif wait == "text":
        chip = "等待文本模型"
        note = f"{chip}。{detail}" if detail else chip
    elif wait == "visual":
        chip = "等待视觉模型"
        note = f"{chip}。{detail}" if detail else chip
    elif wait == "env":
        chip = "等待环境"
        note = f"{chip}。{detail}" if detail else chip
    elif step == "beats" and beat_windows is not None:
        chip = f"拍表 {int(beat_window or 0)}/{int(beat_windows)}"
        note = chip
    else:
        chip = labels[step]
        note = chip
    job["note"] = note
    job["understand_progress"] = {
        "step": step,
        "chip": chip,
        "wait": wait,
        "detail": detail or "",
        "window": int(beat_window or 0) if beat_windows is not None else None,
        "windows": int(beat_windows) if beat_windows is not None else None,
        "window_t0": t0,
        "window_t1": t1,
        "window_failed": nfail,
        "step_started_at": started,
        "steps": steps,
    }
    raise_if_cancelled(directory)
    save_status(job, directory)


def run_understand(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    if _events_done(directory):
        mark_stage(job, directory, "understand", "done")
        job["state"] = "paused"
        job["stage"] = "script"
        job["note"] = "拍表与事件已完成；不跑复刻向 VL。脚本阶段尚未实现"
        save_status(job, directory)
        return job

    missing = _missing_local(settings)
    if missing:
        reason = "理解阶段等待本地依赖：" + "；".join(missing)
        mark_stage(job, directory, "understand", "waiting", error="；".join(missing))
        job["state"] = "paused"
        set_understand_progress(job, directory, step="shots", wait="env", detail=reason)
        return job

    mark_stage(job, directory, "understand", "running")
    set_understand_progress(job, directory, step="shots")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    log = directory / "logs" / "understand.log"
    video = _video_path(directory)
    try:
        probe = job.get("source", {}).get("probe") or probe_video(video)
        duration = float(probe["duration"])
        raise_if_cancelled(directory)

        shots_path = directory / "shots.json"
        shots_doc = _load_json(shots_path)
        if not shots_doc or not shots_doc.get("shots"):
            threshold = float(settings.default.get("scene_threshold") or 27.0)
            shots = detect_shots(video, threshold=threshold)
            shots_doc = {"duration": duration, "shots": shots}
            _write_json(shots_path, shots_doc)
            _log(directory, f"shots {len(shots)}")

        raise_if_cancelled(directory)
        set_understand_progress(job, directory, step="ocr")
        ocr_path = directory / "ocr" / "ocr.json"
        ocr = _load_json(ocr_path)
        if not ocr:
            ocr = run_ocr(video, directory, settings, probe=probe, log_path=log)
            _write_json(ocr_path, ocr)
            _log(directory, f"ocr items={len(ocr.get('items') or [])}")

        raise_if_cancelled(directory)
        set_understand_progress(job, directory, step="asr")
        wav = directory / "source" / "audio.wav"
        full_path = directory / "transcript.full.json"
        transcript_path = directory / "transcript.json"
        full = _load_json(full_path)
        transcript = _load_json(transcript_path)
        if not (full or {}).get("text"):
            legacy = _load_json(directory / "transcript.asr.json")
            if legacy and str(legacy.get("engine") or "").lower().find("qwen3") >= 0:
                full = to_full_transcript(legacy)
                if full.get("text"):
                    _write_json(full_path, full)
                    _log(directory, f"从 transcript.asr.json 写出 transcript.full.json words={len(full.get('words') or [])}")
        if transcript_stale(transcript, settings) or not (full or {}).get("text"):
            if transcript:
                _log(
                    directory,
                    f"转写过期 source={transcript.get('source')}，按 {settings.default.get('asr_model')} 重跑",
                )
            extract_wav(video, wav, log_path=log)
            prompt = _asr_prompt(directory, ocr)
            raw = transcribe_wav(wav, settings, initial_prompt=prompt)
            full = {
                "engine": raw.get("engine"),
                "model": raw.get("model"),
                "aligner": raw.get("aligner"),
                "source": raw.get("source"),
                "aligner_source": raw.get("aligner_source"),
                "device": raw.get("device"),
                "language": raw.get("language"),
                "duration": raw.get("duration"),
                "initial_prompt": raw.get("initial_prompt") or "",
                "text": raw.get("text") or "",
                "words": raw.get("words") or [],
            }
            _write_json(full_path, full)
            for stale_path in (directory / "dialogue.json", directory / "cuts.json"):
                stale_path.unlink(missing_ok=True)
        transcript = finalize_transcript(full or {}, ocr)
        raise_if_cancelled(directory)
        if needs_ocr_asr_judge(transcript):
            ok_llm, llm_detail = llm_health(settings)
            if not ok_llm:
                reason = f"转写已完成，等待文本 LLM 裁定 OCR/ASR。{llm_detail}"
                mark_stage(job, directory, "understand", "waiting", error=llm_detail)
                job["state"] = "paused"
                set_understand_progress(job, directory, step="asr", wait="text", detail=reason)
                return job
        transcript = adjudicate_ocr_asr(
            settings,
            transcript,
            log=lambda text: _log(directory, text),
        )
        _write_json(transcript_path, transcript)
        n_fix = sum(1 for s in (transcript.get("segments") or []) if s.get("source") == "ocr")
        n_extra = sum(1 for s in (transcript.get("segments") or []) if s.get("source") == "asr-extra")
        _log(
            directory,
            f"转写 {transcript_path.resolve()} source={transcript.get('source')} "
            f"device={transcript.get('device')} segs={len(transcript.get('segments') or [])} "
            f"ocr条={n_fix} asr-extra={n_extra}",
        )

        raise_if_cancelled(directory)
        set_understand_progress(job, directory, step="dialogue")
        dialogue_path = directory / "dialogue.json"
        existing_dialogue = _load_json(dialogue_path)
        dialogue = merge_dialogue(transcript, ocr)
        reused_ser = _reuse_ser(existing_dialogue, dialogue)
        if reused_ser:
            dialogue = existing_dialogue
        else:
            if not wav.is_file():
                extract_wav(video, wav, log_path=log)
            dialogue = attach_vocal_emotion(dialogue, wav, settings)
            (directory / "cuts.json").unlink(missing_ok=True)
        _copy_emotions(transcript, dialogue)
        _write_json(transcript_path, transcript)
        _write_json(dialogue_path, dialogue)
        wav.unlink(missing_ok=True)
        n_emo = sum(1 for s in (dialogue.get("speech") or []) if s.get("vocal_emotion"))
        _log(directory, f"SER vocal_emotion tagged={n_emo}/{len(dialogue.get('speech') or [])}")
        raise_if_cancelled(directory)

        beats = _load_json(directory / "beats.json")
        if not _beats_complete(beats, duration):
            ok, detail = vl_health(settings)
            if not ok:
                reason = f"口播已完成，等待 8B 静帧拍表。{detail}"
                mark_stage(job, directory, "understand", "waiting", error=detail)
                job["state"] = "paused"
                set_understand_progress(job, directory, step="beats", wait="visual", detail=reason)
                return job
            _log(directory, "开始静帧拍表")
            beats = run_beat_table(
                settings,
                video=video,
                directory=directory,
                duration=duration,
                dialogue=dialogue,
                log_path=log,
                on_window=lambda done, total, t0=None, t1=None, failed=0: set_understand_progress(
                    job,
                    directory,
                    step="beats",
                    window=done,
                    windows=total,
                    window_t0=t0,
                    window_t1=t1,
                    window_failed=failed,
                ),
            )
            n_err = sum(1 for w in (beats.get("windows") or []) if window_failed(w))
            n_win = len(beats.get("windows") or [])
            _log(directory, f"拍表 windows={n_win} errors={n_err}")
            if n_win == 0 or n_err:
                raise UnderstandWaiting(f"视觉理解未完成：{n_err}/{n_win} 个窗口失败，请检查 Cursor SDK 后重试")
            unload_vl()
            if n_win < 3:
                raise UnderstandWaiting("拍表窗太少，resume 会重试")

        ok_llm, llm_detail = llm_health(settings)
        if not ok_llm:
            reason = f"拍表已完成，等待文本 LLM。{llm_detail}"
            mark_stage(job, directory, "understand", "waiting", error=llm_detail)
            job["state"] = "paused"
            set_understand_progress(job, directory, step="events", wait="text", detail=reason)
            return job

        scene_doc = _load_json(directory / "scene_cuts.json") or {}
        scene = [float(x) for x in scene_doc.get("cuts") or []]
        set_understand_progress(job, directory, step="events")
        _log(directory, "Pass A 拆事件")
        raise_if_cancelled(directory)
        events = run_pass_a(
            settings,
            directory=directory,
            duration=duration,
            beats=beats or {},
            dialogue=dialogue,
            scene=scene,
        )
        unload_vl()
        unload_llm()
        vl_cfg = resolve_vl(settings)
        beat_windows = list((beats or {}).get("windows") or [])
        beat_failed = sum(1 for window in beat_windows if window_failed(window))
        understanding = {
            "done": True,
            "path": "beats",
            "duration": duration,
            "windows": len(beat_windows),
            "visual_windows_total": len(beat_windows),
            "visual_windows_ok": len(beat_windows) - beat_failed,
            "visual_windows_failed": beat_failed,
            "visual_success_rate": round((len(beat_windows) - beat_failed) / len(beat_windows), 4) if beat_windows else 0.0,
            "shots": len((shots_doc or {}).get("shots") or []),
            "events": len(events.get("events") or []),
            "speech_segments": len(dialogue.get("speech") or []),
            "on_screen": len(dialogue.get("on_screen") or []),
            "vl_model": str(vl_cfg.get("model") or ""),
            "llm_model": llm_label(settings),
        }
        _write_json(directory / "understanding.json", understanding)
        mark_stage(job, directory, "understand", "done")
        job["state"] = "paused"
        job["stage"] = "script"
        job["note"] = "拍表与事件已完成；不跑复刻向 VL。脚本阶段尚未实现"
        save_status(job, directory)
        return job
    except JobCancelled:
        unload_vl()
        unload_llm()
        raise
    except UnderstandWaiting as exc:
        unload_vl()
        prev = job.get("understand_progress") or {}
        mark_stage(job, directory, "understand", "waiting", error=str(exc))
        job["state"] = "paused"
        set_understand_progress(
            job,
            directory,
            step=str(prev.get("step") or "beats"),
            wait="visual",
            detail=str(exc),
            window=prev.get("window"),
            windows=prev.get("windows"),
            window_t0=prev.get("window_t0"),
            window_t1=prev.get("window_t1"),
            window_failed=prev.get("window_failed"),
        )
        return job
    except (UnderstandError, ProbeError, VLError, LLMError, RuntimeError) as exc:
        unload_vl()
        unload_llm()
        prev = job.get("understand_progress") or {}
        set_understand_progress(
            job,
            directory,
            step=str(prev.get("step") or "shots"),
            failed=True,
            detail=str(exc),
            window=prev.get("window"),
            windows=prev.get("windows"),
            window_t0=prev.get("window_t0"),
            window_t1=prev.get("window_t1"),
            window_failed=prev.get("window_failed"),
        )
        mark_stage(job, directory, "understand", "failed", error=str(exc))
        raise
