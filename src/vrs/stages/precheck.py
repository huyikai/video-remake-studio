"""预检：Pass B 落盘之后、Comfy 开跑之前的静态门。

不调模型。Pass B 写的时候已经自检过 JSON；这里再检一遍真正喂给 H3 的
`.txt`、关键帧文件在不在、JSON 和 txt 有没有改岔。通过后停在 generate，
等人把 `prompts/*.md` 对照审完再 resume。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vrs.h3grid import PATH_KEYFRAMES, PATH_LOCK_ACROSS, normalize_generate_path
from vrs.jobstore import mark_stage, save_status
from vrs.lock import atomic_write_json
from vrs.passb import assemble_txt
from vrs.promptcheck import (
    check_banned,
    check_clip,
    check_coverage,
    check_dupes,
    check_fields,
    check_han,
    check_header,
    check_locks,
    check_mode,
    d_payloads,
    speech_for_clip,
)
from vrs.settings import Settings


class PrecheckError(RuntimeError):
    pass


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "precheck.log"
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


def _norm_txt(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def audit_job(directory: Path) -> dict[str, Any]:
    """纯函数入口：返回 errors / warnings / 每段摘要，不写盘。"""
    errors: list[str] = []
    warnings: list[str] = []
    clips_doc = _load_json(directory / "clips.json")
    prompts_doc = _load_json(directory / "prompts.json")
    dialogue = _load_json(directory / "dialogue.json") or {}
    if not (clips_doc or {}).get("clips"):
        return {"ok": False, "errors": ["缺少 clips.json"], "warnings": [], "clips": []}
    if not (prompts_doc or {}).get("prompts"):
        return {"ok": False, "errors": ["缺少 prompts.json"], "warnings": [], "clips": []}

    clips = list((clips_doc or {})["clips"])
    index = {str(c["id"]): c for c in clips}
    path = normalize_generate_path(
        (prompts_doc or {}).get("generate_path")
        or (clips_doc or {}).get("generate_path")
        or "i2va_turbo"
    )
    wants_keyframe = bool(PATH_KEYFRAMES.get(path))
    docs: list[dict[str, Any]] = []
    bodies: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []

    listed = {str(item.get("clip_id")) for item in (prompts_doc or {}).get("prompts") or []}
    for clip in clips:
        if clip["id"] not in listed:
            errors.append(f"{clip['id']}: clips.json 有这一段，prompts.json 没有")

    for item in (prompts_doc or {}).get("prompts") or []:
        clip_id = str(item.get("clip_id") or "?")
        clip = index.get(clip_id)
        if clip is None:
            errors.append(f"{clip_id}: prompts.json 有这一段，clips.json 没有")
            continue
        seconds = float(clip["h3_seconds"])
        prompt_rel = str(item.get("prompt") or f"prompts/{clip_id}.txt")
        review_rel = str(item.get("review") or f"prompts/{clip_id}.md")
        json_rel = f"prompts/{clip_id}.json"
        txt_path = directory / prompt_rel
        json_path = directory / json_rel
        review_path = directory / review_rel
        if not txt_path.is_file():
            errors.append(f"{clip_id}: 缺 {prompt_rel}，没法喂给 H3")
            continue
        if not json_path.is_file():
            errors.append(f"{clip_id}: 缺 {json_rel}")
            continue
        if not review_path.is_file():
            warnings.append(f"{clip_id}: 缺中文对照 {review_rel}")
        if wants_keyframe:
            key = str(clip.get("keyframe") or f"keyframes/{clip_id}_a.jpg")
            if not (directory / key).is_file():
                errors.append(f"{clip_id}: I2VA 缺首帧 {key}")

        doc = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            errors.append(f"{clip_id}: {json_rel} 不是 JSON 对象")
            continue
        doc["clip_id"] = clip_id
        doc["cast_reset"] = bool(clip.get("cast_reset"))
        txt = txt_path.read_text(encoding="utf-8")
        rebuilt = assemble_txt(doc, clip, path)
        if _norm_txt(txt) != _norm_txt(rebuilt):
            errors.append(f"{clip_id}: {prompt_rel} 和 {json_rel} 改岔了，txt 才是喂给 H3 的，请同步")

        allowed = speech_for_clip(clip, dialogue, clips=clips)
        body = " ".join(str(s.get("text") or "") for s in (doc.get("shots") or []))
        errors += check_clip(doc, seconds, allowed)
        errors += check_header(clip_id, txt, seconds)
        errors += check_han(clip_id, txt)
        errors += check_banned(clip_id, txt)
        errors += check_fields(clip_id, txt)
        errors += check_mode(clip_id, txt, wants_keyframe=wants_keyframe)
        errors += check_coverage(clip_id, body or txt, allowed)
        docs.append(doc)
        bodies.append((clip_id, body or txt))
        rows.append(
            {
                "clip_id": clip_id,
                "h3_seconds": seconds,
                "shots": len(doc.get("shots") or []),
                "speakers": [str(s.get("id") or "") for s in (doc.get("speakers") or [])],
                "lines": d_payloads(body or txt),
                "prompt": prompt_rel,
                "review": review_rel,
            }
        )

    if PATH_LOCK_ACROSS.get(path, True):
        errors += check_locks(docs)
    errors += check_dupes(bodies)
    return {
        "ok": not errors,
        "generate_path": path,
        "errors": errors,
        "warnings": warnings,
        "clips": rows,
    }


def _report_md(audit: dict[str, Any]) -> str:
    head = "通过" if audit["ok"] else "不通过"
    err = "\n".join(f"- {e}" for e in audit["errors"]) or "-（无）"
    warn = "\n".join(f"- {w}" for w in audit["warnings"]) or "-（无）"
    lines = []
    for row in audit.get("clips") or []:
        people = "、".join(row["speakers"]) or "无"
        said = "；".join(f"「{x}」" for x in row["lines"]) or "无对白"
        lines.append(
            f"- `{row['clip_id']}`  {row['h3_seconds']:.2f}s  "
            f"{row['shots']} 镜  {people}  {said}"
        )
    body = "\n".join(lines) or "-（无）"
    return (
        f"# 预检报告\n\n"
        f"结果：**{head}**  错误 {len(audit['errors'])}  警告 {len(audit['warnings'])}  "
        f"路线 `{audit.get('generate_path')}`\n\n"
        f"## 错误\n\n{err}\n\n"
        f"## 警告\n\n{warn}\n\n"
        f"## 各段\n\n{body}\n"
    )


def run_precheck(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    del settings
    if (job.get("stages") or {}).get("script", {}).get("status") != "done":
        raise PrecheckError("脚本阶段还没完成")
    mark_stage(job, directory, "precheck", "running")
    try:
        audit = audit_job(directory)
        atomic_write_json(directory / "precheck.json", audit)
        (directory / "precheck.md").write_text(_report_md(audit), encoding="utf-8")
        n = len(audit.get("clips") or [])
        if not audit["ok"]:
            msg = f"预检不通过：{len(audit['errors'])} 项\n" + "\n".join(audit["errors"][:12])
            _log(directory, msg)
            mark_stage(job, directory, "precheck", "failed", error=msg)
            job["note"] = msg
            save_status(job, directory)
            raise PrecheckError(msg)
        extra = f"，警告 {len(audit['warnings'])}" if audit["warnings"] else ""
        _log(directory, f"预检通过 {n} 段{extra}")
        mark_stage(job, directory, "precheck", "done")
        job["stage"] = "generate"
        mode = str((job.get("options") or {}).get("review_mode") or "pause_draft")
        if mode == "full_auto":
            job["state"] = "running"
            job["note"] = f"预检通过 {n} 段{extra}；自动试片，VL/SDK 质检后再出成片"
        else:
            job["state"] = "paused"
            job["note"] = (
                f"预检通过 {n} 段{extra}；对照在 prompts/*.md，"
                "审完后出试片"
            )
        save_status(job, directory)
        return job
    except PrecheckError:
        raise
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        mark_stage(job, directory, "precheck", "failed", error=str(exc))
        raise
