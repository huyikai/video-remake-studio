"""交付：按源片时长裁 pad → 硬切拼接 → 可选超分 → 烧 ASS → 封面 → 可选 SMTP。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vrs.ass import write_ass
from vrs.cover import write_cover
from vrs.deliver import trim_and_concat
from vrs.h3grid import normalize_generate_path
from vrs.jobstore import mark_stage, save_status
from vrs.lock import atomic_write_json
from vrs.mailer import send_mail
from vrs.media import burn_ass
from vrs.probe import ProbeError
from vrs.settings import Settings
from vrs.stages.generate import clip_output_dir, job_generate_path, quality_complete
from vrs.upscale import maybe_upscale


class FinishError(RuntimeError):
    pass


def _log(directory: Path, text: str) -> None:
    path = directory / "logs" / "finish.log"
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
    clips = list(doc.get("clips") or [])
    if not clips:
        raise FinishError("缺少 clips.json")
    return clips


def _pick_quality(directory: Path, clips: list[dict[str, Any]], path: str) -> str:
    if quality_complete(directory, clips, "final", path=path):
        return "final"
    raise FinishError("各段成片还没齐，不能拼接")


def run_finish(settings: Settings, job: dict[str, Any], directory: Path) -> dict[str, Any]:
    if (job.get("stages") or {}).get("generate", {}).get("status") != "done":
        raise FinishError("生成还没完成")
    clips = _clips(directory)
    path = job_generate_path(directory, job)
    path = normalize_generate_path(path)
    quality = _pick_quality(directory, clips, path)
    log = directory / "logs" / "finish.log"
    src_dir = clip_output_dir(directory, path, quality)
    out_dir = directory / "output" / path
    mark_stage(job, directory, "finish", "running")
    try:
        raw = out_dir / f"{quality}.raw.mp4"
        trim_and_concat(
            clips,
            src_dir=src_dir,
            dest=raw,
            work_dir=src_dir / "trimmed",
            log_path=log,
        )
        _log(directory, f"裁 pad 后拼接 {len(clips)} 段 → {raw.relative_to(directory)}")
        scaled = maybe_upscale(
            settings,
            raw,
            out_dir / f"{quality}.up.mp4",
            log=lambda text: _log(directory, text),
        )
        dest = out_dir / f"{quality}.mp4"
        dialogue = _load_json(directory / "dialogue.json") or {}
        ass_events = 0
        burned_ass = False
        if bool(settings.default.get("ass_burn", True)):
            ass_path = out_dir / f"{quality}.ass"
            ass_events = write_ass(
                ass_path,
                dialogue,
                clips,
                default_region=str(settings.default.get("ass_default_region") or "bottom"),
            )
            burned = out_dir / f"{quality}.burn.mp4"
            burn_ass(scaled, ass_path, burned, log_path=log)
            dest.unlink(missing_ok=True)
            burned.replace(dest)
            burned_ass = True
            _log(directory, f"烧 ASS {ass_events} 条 → {dest.relative_to(directory)}")
        elif scaled.resolve() != dest.resolve():
            dest.write_bytes(scaled.read_bytes())
        cover = out_dir / "cover.jpg"
        title = str((job.get("source") or {}).get("url") or job.get("id") or "")
        how = write_cover(
            settings,
            cover,
            video=dest,
            title=title,
            log=lambda text: _log(directory, text),
        )
        rel = f"output/{path}/{quality}.mp4"
        atomic_write_json(
            directory / "finish.json",
            {
                "ok": True,
                "quality": quality,
                "clips": len(clips),
                "concat": True,
                "ass_burn": burned_ass,
                "ass_events": ass_events,
                "cover": how,
                "file": rel,
            },
        )
        send_mail(
            settings.smtp,
            subject=f"VRS 完成 {job['id']}（拼接成片）",
            body=(
                f"job {job['id']}\nquality {quality}\nclips {len(clips)}\n"
                f"ass {ass_events}\nvideo {dest.resolve()}\ncover {cover.resolve()} ({how})"
            ),
            attachments=[dest],
            log=lambda text: _log(directory, text),
        )
        mark_stage(job, directory, "finish", "done")
        job["state"] = "done"
        job["stage"] = "finish"
        ass_note = f"，烧字 {ass_events} 条" if burned_ass else "，未烧字"
        job["note"] = f"拼接成片完成：{rel}（{len(clips)} 段{ass_note}，封面 {how}）"
        save_status(job, directory)
        return job
    except (FinishError, ProbeError, OSError) as exc:
        mark_stage(job, directory, "finish", "failed", error=str(exc))
        job["note"] = str(exc)
        save_status(job, directory)
        raise
