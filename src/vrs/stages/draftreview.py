"""full_auto：试片对照原片，VL 打分，SDK 改提示词，最多返工两轮。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from vrs.cancel import raise_if_cancelled
from vrs.comfyclient import free_vram
from vrs.h3grid import normalize_generate_path
from vrs.jobops import _write_prompt_files
from vrs.jobstore import save_status
from vrs.llmclient import LLMError, generate_text, unload_llm
from vrs.lock import atomic_write_json
from vrs.media import extract_frame_at
from vrs.settings import Settings
from vrs.stages.generate import clip_output_dir, job_generate_path, quality_complete, run_generate
from vrs.stages.precheck import audit_job
from vrs.textjson import parse_json_payload
from vrs.vlclient import VLError, analyze_images, unload_vl

PASS_SCORE = 7.0
DEFAULT_ROUNDS = 2

_COMPARE_PROMPT = """\
你在给镜像翻拍做试片质检。图片顺序：源片开头、源片中间、源片结尾、试片开头、试片中间、试片结尾。
对照的是同一段故事时间，不是整部片子。
只根据看见的画面判断，不要编没看见的东西。

只输出一个 JSON 对象：
{
  "clip_id": "%s",
  "pass": true,
  "score": 8.0,
  "identity": 8,
  "blocking": 8,
  "camera": 8,
  "action": 8,
  "scene": 8,
  "issues": ["短句，写具体偏差"],
  "fix_hint": "给写稿模型的修改方向，一到三句"
}
score / 分项都是 1-10。pass 为 true 的条件：score>=7 且 identity>=6 且 scene>=6，并且人物数量、场景地点没有明显跑偏。
禁止输出 JSON 以外的文字。
"""

_PATCH_PROMPT = """\
你在修 MiniMax H3 试片不合格的提示词。不要改变 clip_id、时长、说话人 id、镜号结构。
只改正 VL 指出的偏差：站位、朝向、景别、动作链、场景锁。对白 <d>[Chinese] …</d> 不要改。
不要加字幕、不要写情绪分数、不要发明新角色。

当前 clip_id={clip_id}
VL 结论：
{review}

现有提示词 JSON：
{prompt}

只输出一个 JSON 对象：
{{"clip_id":"{clip_id}","action":"patch","zh":{{}},"scene_lock":"","speakers":[],"shots":[{{"index":1,"at":null,"text":"..."}}]}}
action 也可以是 keep（认为改不了或已经够了）。
patch 时 speakers/shots 必须覆盖原有每一项；text 保持 H3 英文镜锁格式。
"""


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "draftreview.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")
    print(text, flush=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _clips(directory: Path) -> list[dict[str, Any]]:
    doc = _load_json(directory / "clips.json") or {}
    return list(doc.get("clips") or [])


def _three_times(t0: float, t1: float) -> tuple[float, float, float]:
    span = max(0.05, float(t1) - float(t0))
    return (float(t0) + span * 0.08, float(t0) + span * 0.5, max(float(t0), float(t1) - span * 0.08))


def _grab(video: Path, dest: Path, seconds: float, log: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        extract_frame_at(video, dest, seconds, log_path=log)
    except Exception:
        return None
    return dest if dest.is_file() else None


def _review_clip(
    settings: Settings,
    directory: Path,
    clip: dict[str, Any],
    *,
    source: Path,
    draft: Path,
) -> dict[str, Any]:
    clip_id = str(clip["id"])
    t0 = float(clip.get("t0") or 0)
    t1 = float(clip.get("t1") or t0)
    work = directory / "draft_review" / clip_id
    log = directory / "logs" / "draftreview.log"
    src_t = _three_times(t0, t1)
    try:
        from vrs.probe import probe_video

        dur = float(probe_video(draft)["duration"])
    except Exception:  # noqa: BLE001
        dur = max(0.2, t1 - t0)
    dr_t = _three_times(0.0, dur)
    frames: list[Path] = []
    for index, (label, video, ts) in enumerate(
        (
            ("src0", source, src_t[0]),
            ("src1", source, src_t[1]),
            ("src2", source, src_t[2]),
            ("dr0", draft, dr_t[0]),
            ("dr1", draft, dr_t[1]),
            ("dr2", draft, dr_t[2]),
        )
    ):
        grabbed = _grab(video, work / f"{label}.jpg", ts, log)
        if grabbed is not None:
            frames.append(grabbed)
    if len(frames) < 4:
        return {
            "clip_id": clip_id,
            "pass": False,
            "score": 0,
            "issues": ["抽帧不足，无法对照"],
            "fix_hint": "",
            "error": "need_frames",
        }
    prompt = _COMPARE_PROMPT % clip_id
    try:
        raw = analyze_images(
            settings,
            frames,
            prompt,
            max_new_tokens=int(settings.default.get("vl_review_max_tokens") or 900),
            max_edge=int(settings.default.get("vl_review_max_edge") or 768),
        )
        data = parse_json_payload(raw, require="clip_id")
    except (VLError, ValueError) as exc:
        return {
            "clip_id": clip_id,
            "pass": True,
            "score": None,
            "issues": [f"VL 无法判定，本段放行：{exc}"],
            "fix_hint": "",
            "error": str(exc),
        }
    if not isinstance(data, dict):
        return {"clip_id": clip_id, "pass": True, "score": None, "issues": ["VL 输出不是对象"], "error": "shape"}
    score = data.get("score")
    try:
        score_f = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score_f = 0.0
    identity = float(data.get("identity") or 0)
    scene = float(data.get("scene") or 0)
    passed = bool(data.get("pass")) and score_f >= PASS_SCORE and identity >= 6 and scene >= 6
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    return {
        "clip_id": clip_id,
        "pass": passed,
        "score": score_f,
        "identity": identity,
        "blocking": data.get("blocking"),
        "camera": data.get("camera"),
        "action": data.get("action"),
        "scene": scene,
        "issues": [str(x) for x in issues][:8],
        "fix_hint": str(data.get("fix_hint") or ""),
    }


def _merge_prompt(original: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(original)
    if isinstance(patch.get("zh"), dict):
        zh = dict(out.get("zh") or {})
        zh.update(patch["zh"])
        out["zh"] = zh
    if isinstance(patch.get("scene_lock"), str) and patch["scene_lock"].strip():
        out["scene_lock"] = patch["scene_lock"]
    if isinstance(patch.get("speakers"), list) and patch["speakers"]:
        out["speakers"] = patch["speakers"]
    if isinstance(patch.get("shots"), list) and patch["shots"]:
        by_index = {int(s.get("index") or i + 1): s for i, s in enumerate(out.get("shots") or [])}
        merged = []
        for item in patch["shots"]:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("index") or 0)
            base = dict(by_index.get(idx) or {})
            base.update({k: v for k, v in item.items() if v is not None})
            merged.append(base)
        if merged:
            out["shots"] = merged
    return out


def _patch_failed(
    settings: Settings,
    directory: Path,
    job: dict[str, Any],
    failed: list[dict[str, Any]],
) -> list[str]:
    clips_doc = _load_json(directory / "clips.json") or {}
    index = {str(c["id"]): c for c in clips_doc.get("clips") or []}
    path = normalize_generate_path(
        clips_doc.get("generate_path") or (job.get("options") or {}).get("generate_path") or "t2va"
    )
    changed: list[str] = []
    for row in failed:
        clip_id = str(row.get("clip_id") or "")
        clip = index.get(clip_id)
        json_path = directory / "prompts" / f"{clip_id}.json"
        if clip is None or not json_path.is_file():
            continue
        original = json.loads(json_path.read_text(encoding="utf-8"))
        backup = json_path.read_text(encoding="utf-8")
        prompt = _PATCH_PROMPT.format(
            clip_id=clip_id,
            review=json.dumps(row, ensure_ascii=False, indent=2),
            prompt=json.dumps(original, ensure_ascii=False),
        )
        try:
            raw = generate_text(settings, prompt, thinking=False)
            data = parse_json_payload(raw, require="action")
        except (LLMError, ValueError) as exc:
            _log(directory, f"{clip_id} SDK 改词失败：{exc}")
            continue
        if not isinstance(data, dict) or str(data.get("action") or "keep") == "keep":
            _log(directory, f"{clip_id} SDK 选择 keep")
            continue
        merged = _merge_prompt(original, data)
        merged["clip_id"] = clip_id
        _write_prompt_files(directory, clip, merged, path)
        audit = audit_job(directory)
        if not audit.get("ok"):
            json_path.write_text(backup, encoding="utf-8")
            _write_prompt_files(directory, clip, original, path)
            _log(directory, f"{clip_id} 改词后预检不通过，已回滚：{audit.get('errors')}")
            continue
        changed.append(clip_id)
        _log(directory, f"{clip_id} 已按 VL 意见改词")
    return changed


def _delete_drafts(directory: Path, job: dict[str, Any], clip_ids: list[str]) -> None:
    path = job_generate_path(directory, job)
    dest_dir = clip_output_dir(directory, path, "draft")
    for clip_id in clip_ids:
        (dest_dir / f"{clip_id}.mp4").unlink(missing_ok=True)
    concat = directory / "output" / path / "draft.mp4"
    concat.unlink(missing_ok=True)
    trimmed = dest_dir / "trimmed"
    if trimmed.is_dir():
        shutil.rmtree(trimmed, ignore_errors=True)


def run_auto_review(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    clips = _clips(directory)
    path = job_generate_path(directory, job)
    if not clips or not quality_complete(directory, clips, "draft", path=path):
        return job
    existing = _load_json(directory / "draft_review.json") or {}
    prior = list(existing.get("rounds") or [])
    if prior:
        last_rows = list((prior[-1] or {}).get("clips") or [])
        if last_rows and all(row.get("pass") for row in last_rows):
            job["note"] = "试片质检已通过，出成片"
            save_status(job, directory)
            return job
        if existing.get("exhausted"):
            job["note"] = "试片质检已满轮次，继续成片"
            save_status(job, directory)
            return job
    free_vram(settings)
    rounds = int((job.get("options") or {}).get("draft_review_rounds") or DEFAULT_ROUNDS)
    rounds = max(1, min(rounds, 4))
    source = directory / "source" / "video.mp4"
    history: list[dict[str, Any]] = []
    dest_dir = clip_output_dir(directory, path, "draft")

    job["state"] = "running"
    for round_i in range(1, rounds + 1):
        raise_if_cancelled(directory)
        job["note"] = f"VL 对照试片（第 {round_i}/{rounds} 轮）"
        job["stage"] = "generate"
        save_status(job, directory)
        _log(directory, job["note"])
        rows: list[dict[str, Any]] = []
        for clip in clips:
            raise_if_cancelled(directory)
            clip_id = str(clip["id"])
            draft = dest_dir / f"{clip_id}.mp4"
            if not draft.is_file():
                rows.append({"clip_id": clip_id, "pass": False, "issues": ["缺少试片文件"]})
                continue
            row = _review_clip(settings, directory, clip, source=source, draft=draft)
            rows.append(row)
            _log(
                directory,
                f"{clip_id} pass={row.get('pass')} score={row.get('score')} issues={row.get('issues')}",
            )
        unload_vl()
        report = {"round": round_i, "clips": rows}
        history.append(report)
        atomic_write_json(directory / "draft_review.json", {"rounds": history})
        failed = [row for row in rows if not row.get("pass")]
        if not failed:
            job["note"] = f"试片质检通过（第 {round_i} 轮），出成片"
            save_status(job, directory)
            return job
        job["note"] = f"试片 {len(failed)}/{len(rows)} 段未过，SDK 改词"
        save_status(job, directory)
        changed = _patch_failed(settings, directory, job, failed)
        unload_llm()
        redo = changed or [str(row.get("clip_id")) for row in failed if row.get("clip_id")]
        redo = [c for c in redo if c]
        if not redo:
            _log(directory, "没有可重跑的段，结束质检")
            job["note"] = "试片质检无法改词，继续成片"
            save_status(job, directory)
            return job
        _delete_drafts(directory, job, redo)
        gen = (job.get("stages") or {}).get("generate") or {}
        if gen.get("status") == "done":
            gen["status"] = "pending"
            gen["error"] = None
            save_status(job, directory)
        job["note"] = f"重跑试片 {', '.join(redo)}"
        save_status(job, directory)
        job = run_generate(settings, job, directory, quality="draft")
        if job.get("stages", {}).get("generate", {}).get("status") != "done":
            return job

    job["note"] = f"试片质检已满 {rounds} 轮，继续成片"
    save_status(job, directory)
    atomic_write_json(directory / "draft_review.json", {"rounds": history, "exhausted": True})
    return job
