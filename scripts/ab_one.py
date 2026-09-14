"""A/B 单段对比：附录版提示词重写一段 → 同 seed 渲试片 → 配对旧试片。

用法: uv run python scripts/ab_one.py h3_04 [--dry]
--dry 只构建提示词不发 LLM，用于烟测。
产物: data/jobs/<job>/_ab/<cid>_old.mp4 / <cid>_new.mp4 / <cid>_new.txt
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vrs.comfyclient import load_workflow, object_info, run_h3_clip
from vrs.llmclient import generate_text
from vrs.passb import _apply_canonical_locks, _classify_thread, _spread, assemble_txt, build_prompt, clip_facts
from vrs.promptcheck import check_clip, check_header, check_mode, check_mode as _cm, speech_for_clip
from vrs.settings import Settings
from vrs.stages.generate import _quality_params, _seed
from vrs.textjson import parse_json_payload

JOB = "20260911-160851-36f4"
MAX_ATTEMPTS = 5


def group_locks(clips: list[dict], target_id: str, prompts_dir: Path) -> dict[str, str]:
    """模拟故事组锁传递：目标段之前的同组成员依次钉锁。"""
    locks: dict[str, str] = {}
    for clip in clips:
        cid = str(clip["id"])
        if cid == target_id:
            break
        if clip.get("cast_reset"):
            locks.clear()
        doc_path = prompts_dir / f"{cid}.json"
        if doc_path.is_file():
            try:
                doc = json.loads(doc_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            _apply_canonical_locks(doc, locks)
    return locks


def main() -> None:
    clip_id = sys.argv[1]
    dry = "--dry" in sys.argv
    d = Path("data/jobs") / JOB
    s = Settings()
    clips = json.loads((d / "clips.json").read_text(encoding="utf-8"))["clips"]
    beats = json.loads((d / "beats.json").read_text(encoding="utf-8"))
    dia = json.loads((d / "dialogue.json").read_text(encoding="utf-8"))
    cuts = [float(c) for c in (json.loads((d / "scene_cuts.json").read_text(encoding="utf-8")).get("cuts") or [])]
    clip = next(x for x in clips if str(x["id"]) == clip_id)
    facts = clip_facts(clip, beats, dia, cuts, root=d, clips=clips)
    allowed = speech_for_clip(clip, dia, clips=clips)
    seconds = float(clip["h3_seconds"])
    locks = group_locks(clips, clip_id, d / "prompts")
    images = [p for _t, p in _spread(list(facts["frames"] or []), 4)]

    verdict = _classify_thread(s, facts)
    if verdict:
        facts["thread_verdict"] = verdict
        print(json.dumps({"clip": clip_id, "thread_verdict": verdict}, ensure_ascii=False))

    prompt = build_prompt(clip, facts, path="t2va", locks=locks)
    print(json.dumps({"clip": clip_id, "prompt_chars": len(prompt), "locks": sorted(locks)}, ensure_ascii=False))
    if dry:
        return

    errors: list[str] = []
    doc = None
    txt = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        p = build_prompt(clip, facts, path="t2va", locks=locks, errors=errors or None)
        stats: dict = {}
        raw = generate_text(s, p, images=images or None, stats=stats)
        cand = parse_json_payload(raw, require="shots")
        if not isinstance(cand, dict):
            errors = ["返回的不是 JSON 对象"]
            continue
        cand["clip_id"] = clip_id
        cand["generate_path"] = "t2va"
        cand_txt = assemble_txt(cand, clip, "t2va")
        errors = (
            check_clip(cand, seconds, allowed)
            + check_header(clip_id, cand_txt, seconds)
            + check_mode(clip_id, cand_txt, wants_keyframe=False)
        )
        print(json.dumps({"clip": clip_id, "attempt": attempt, "errors": errors[:3]}, ensure_ascii=False))
        if not errors:
            doc, txt = cand, cand_txt
            break
    if doc is None or txt is None:
        raise SystemExit(f"{clip_id}: {MAX_ATTEMPTS} 次仍未过机检: {errors[:2]}")

    ab = d / "_ab"
    ab.mkdir(exist_ok=True)
    (ab / f"{clip_id}_new.txt").write_text(txt, encoding="utf-8")
    (ab / f"{clip_id}_new.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    job = json.loads((d / "status.json").read_text(encoding="utf-8"))
    params = _quality_params(s, "t2va", "draft", job)
    info = object_info(s, cache={})
    wf = load_workflow(s, str(params["workflow"]))
    _, dest = run_h3_clip(
        s, workflow=wf, info=info, prompt_text=txt,
        seconds=seconds, steps=int(params["steps"]), megapixels=float(params["megapixels"]),
        aspect="16:9", seed=_seed(job["id"], clip_id, "draft"),
        filename_prefix=f"ab/{clip_id}_new", dest=ab / f"{clip_id}_new.mp4", timeout=1800.0,
    )
    old = d / "generate/t2va/draft" / f"{clip_id}.mp4"
    shutil.copyfile(old, ab / f"{clip_id}_old.mp4")
    print(json.dumps({
        "clip": clip_id, "ok": True,
        "old_bytes": (ab / f"{clip_id}_old.mp4").stat().st_size,
        "new_bytes": dest.stat().st_size,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
