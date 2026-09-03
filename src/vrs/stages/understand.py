from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from vrs.asr import resolve_aligner_model, resolve_asr_model, transcribe_wav, transcript_stale
from vrs.beats import run_beat_table
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
    if not beats or not beats.get("windows"):
        return False
    return True


def _beats_complete(beats: dict[str, Any] | None, duration: float) -> bool:
    if not beats:
        return False
    windows = beats.get("windows") or []
    if not windows:
        return False
    if any(w.get("error") for w in windows):
        # 跑失败的窗会在该段里断掉「人物组变化」这条信号，resume 时要重问
        return False
    last = max(float(w.get("end") or 0) for w in windows)
    if last < duration - 0.6:
        return False
    hop = float(beats.get("hop") or 2.5)
    starts = sorted(float(w.get("start") or 0) for w in windows)
    return all(b - a <= hop + 0.1 for a, b in zip(starts, starts[1:]))


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
        mark_stage(job, directory, "understand", "waiting", error="；".join(missing))
        job["state"] = "paused"
        job["note"] = "理解阶段等待本地依赖：" + "；".join(missing)
        save_status(job, directory)
        return job

    mark_stage(job, directory, "understand", "running")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    log = directory / "logs" / "understand.log"
    video = _video_path(directory)
    try:
        probe = job.get("source", {}).get("probe") or probe_video(video)
        duration = float(probe["duration"])

        shots_path = directory / "shots.json"
        shots_doc = _load_json(shots_path)
        if not shots_doc or not shots_doc.get("shots"):
            threshold = float(settings.default.get("scene_threshold") or 27.0)
            shots = detect_shots(video, threshold=threshold)
            shots_doc = {"duration": duration, "shots": shots}
            _write_json(shots_path, shots_doc)
            _log(directory, f"shots {len(shots)}")

        ocr_path = directory / "ocr" / "ocr.json"
        ocr = _load_json(ocr_path)
        if not ocr:
            ocr = run_ocr(video, directory, settings, probe=probe, log_path=log)
            _write_json(ocr_path, ocr)
            _log(directory, f"ocr items={len(ocr.get('items') or [])}")

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
        if needs_ocr_asr_judge(transcript):
            ok_llm, llm_detail = llm_health(settings)
            if not ok_llm:
                mark_stage(job, directory, "understand", "waiting", error=llm_detail)
                job["state"] = "paused"
                job["note"] = f"转写已完成，等待文本 LLM 裁定 OCR/ASR。{llm_detail}"
                save_status(job, directory)
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

        beats = _load_json(directory / "beats.json")
        if not _beats_complete(beats, duration):
            ok, detail = vl_health(settings)
            if not ok:
                mark_stage(job, directory, "understand", "waiting", error=detail)
                job["state"] = "paused"
                job["note"] = f"口播已完成，等待 8B 静帧拍表。{detail}"
                save_status(job, directory)
                return job
            _log(directory, "开始静帧拍表")
            beats = run_beat_table(
                settings,
                video=video,
                directory=directory,
                duration=duration,
                dialogue=dialogue,
                log_path=log,
            )
            n_err = sum(1 for w in (beats.get("windows") or []) if w.get("error"))
            n_win = len(beats.get("windows") or [])
            _log(directory, f"拍表 windows={n_win} errors={n_err}")
            unload_vl()
            if n_win < 3:
                raise UnderstandWaiting("拍表窗太少，resume 会重试")

        ok_llm, llm_detail = llm_health(settings)
        if not ok_llm:
            mark_stage(job, directory, "understand", "waiting", error=llm_detail)
            job["state"] = "paused"
            job["note"] = f"拍表已完成，等待文本 LLM。{llm_detail}"
            save_status(job, directory)
            return job

        scene_doc = _load_json(directory / "scene_cuts.json") or {}
        scene = [float(x) for x in scene_doc.get("cuts") or []]
        _log(directory, "Pass A 拆事件")
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
        understanding = {
            "done": True,
            "path": "beats",
            "duration": duration,
            "windows": len((beats or {}).get("windows") or []),
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
    except UnderstandWaiting as exc:
        unload_vl()
        mark_stage(job, directory, "understand", "waiting", error=str(exc))
        job["state"] = "paused"
        job["note"] = str(exc)
        save_status(job, directory)
        return job
    except (UnderstandError, ProbeError, VLError, LLMError, RuntimeError) as exc:
        unload_vl()
        unload_llm()
        mark_stage(job, directory, "understand", "failed", error=str(exc))
        raise
