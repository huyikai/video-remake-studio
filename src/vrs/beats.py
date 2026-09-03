from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from vrs.lock import atomic_write_json
from vrs.media import extract_frame_at
from vrs.probe import which_ffmpeg
from vrs.settings import Settings
from vrs.textjson import parse_json_payload
from vrs.vlclient import VLError, analyze_image, unload_vl

WINDOW = 3.0
HOP = 2.5
STEP = 0.25
CELLS = 12
CELL_H = 360
CUT_NEAR = 0.25
SCENE = 0.25

BEAT_PROMPT = """\
你在替一条要复刻的原片做读图。只描述这一窗里实际看见的。
不要写剧本，不要判定谁在说话，不要写「说话人」。
画面上谁嘴在动，不等于字幕上那句就是他说的。

这张图是同一时间窗的 {n} 帧横向拼接，从左到右每隔 {step:.2f} 秒。
整窗 {start:.2f}s–{end:.2f}s。不是 {n} 个镜头，也不是 {n} 个人。
各格时间（秒）：{cell_list}

只输出一个 JSON 对象（不要数组，不要 markdown，不要其它段落）。字段：
- cells: 数组，每项 {{"t": 秒（纯数字，不要汉字）, "see": "人/衣/站位/手/脸/道具，一句，40–80 字"}}
  同一句话不要重复写。人多写清人数和左右站位，不要用一句情节摘要代替看见的东西。
- adults: 整数
- children: 整数
- mouth: 数组，每项 {{"who": "左|中|右或衣着", "state": "open|closed|offscreen", "cells": "起止格秒数"}}
- has_text: 布尔，画面上是否有字（不要抄写原文）
- action: 按看见→靠近→接触→结果写，不要收成情节摘要，不要编没看见的
- unsure: 字符串数组，没有则 []
"""


def _cs(t: float) -> str:
    return f"{int(round(t * 100)):05d}"


def _round(t: float) -> float:
    return round(float(t), 2)


def window_starts(duration: float, cuts: list[float], *, hop: float = HOP) -> list[float]:
    starts: list[float] = []
    t = 0.0
    while t < duration - 0.05:
        starts.append(_round(t))
        t += hop
    for cut in cuts:
        if cut <= 0 or cut >= duration:
            continue
        if any(abs(s - cut) <= CUT_NEAR for s in starts):
            continue
        starts.append(_round(cut))
    return sorted(set(starts))


def cell_times(start: float, end: float, *, step: float = STEP, cells: int = CELLS) -> list[float]:
    times: list[float] = []
    t = start
    while t < end - 1e-6 and len(times) < cells:
        times.append(_round(t))
        t += step
    return times or [_round(start)]


def scene_cuts(video: Path, *, threshold: float = SCENE) -> list[float]:
    completed = subprocess.run(
        [
            which_ffmpeg(),
            "-i",
            str(video),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    cuts: list[float] = []
    for line in (completed.stderr or "").splitlines():
        if "pts_time:" not in line or "showinfo" not in line:
            continue
        try:
            cuts.append(float(line.split("pts_time:")[1].split()[0]))
        except (IndexError, ValueError):
            continue
    return cuts


def make_strip(cells: list[Path], dest: Path, *, height: int = CELL_H) -> Path | None:
    from PIL import Image

    images = []
    for path in cells:
        if not path.is_file():
            continue
        im = Image.open(path).convert("RGB")
        if im.height != height:
            width = max(1, round(im.width * height / im.height))
            im = im.resize((width, height), Image.Resampling.LANCZOS)
        images.append(im)
    if not images:
        return None
    canvas = Image.new("RGB", (sum(im.width for im in images), height))
    x = 0
    for im in images:
        canvas.paste(im, (x, 0))
        x += im.width
        im.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, quality=85)
    canvas.close()
    return dest


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - 1e-6 and b0 < a1 - 1e-6


def _window_items(items: list[dict[str, Any]], start: float, end: float, *, t0="t0", t1="t1") -> list[dict[str, Any]]:
    out = []
    for item in items:
        s = float(item.get(t0) or item.get("t") or 0)
        e = float(item.get(t1) or item.get("t") or s)
        if e <= s:
            e = s + 0.05
        if _overlap(start, end, s, e):
            out.append(item)
    return out


def _ocr_in_window(dialogue: dict[str, Any], start: float, end: float) -> bool:
    for item in dialogue.get("on_screen") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        s = float(item.get("t0") or 0)
        e = float(item.get("t1") or s)
        if _overlap(start, end, s, e):
            return True
    return False


def _speech_in_window(dialogue: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    return _window_items(list(dialogue.get("speech") or []), start, end)


def window_has_cut(start: float, end: float, cuts: list[float]) -> bool:
    return any(start + 0.04 < c < end - 0.04 for c in cuts)


def flag_window(
    win: dict[str, Any],
    *,
    prev: dict[str, Any] | None,
    cuts: list[float],
    dialogue: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    start, end = float(win["start"]), float(win["end"])
    if win.get("error"):
        flags.append("跑失败")
        return flags
    if window_has_cut(start, end, cuts):
        flags.append("窗内硬切")
    people = int(win.get("adults") or 0) + int(win.get("children") or 0)
    if prev and prev.get("adults") is not None and win.get("adults") is not None:
        prev_n = int(prev.get("adults") or 0) + int(prev.get("children") or 0)
        if abs(people - prev_n) >= 1:
            flags.append("人数变了")
    if len(_speech_in_window(dialogue, start, end)) >= 2:
        flags.append("对白多句")
    ocr_has = _ocr_in_window(dialogue, start, end)
    has_text = bool(win.get("has_text"))
    if ocr_has != has_text:
        flags.append("OCR与画面字对不上")
    return flags


def _as_beat_obj(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        merged: dict[str, Any] = {}
        cell_rows = [x for x in data if "see" in x or ("t" in x and "adults" not in x)]
        field_rows = [x for x in data if "adults" in x or "action" in x or "has_text" in x]
        for item in field_rows:
            merged.update(item)
        if cell_rows and "cells" not in merged:
            merged["cells"] = cell_rows
        if merged:
            return merged
    raise ValueError("拍表不是对象")


def _parse_beat(raw: str, cells: list[float]) -> dict[str, Any]:
    data = _as_beat_obj(parse_json_payload(raw))
    adults = data.get("adults")
    children = data.get("children")
    try:
        adults_n = int(adults) if adults is not None else None
        children_n = int(children) if children is not None else None
    except (TypeError, ValueError):
        adults_n, children_n = None, None
    see = []
    for item in data.get("cells") or []:
        if not isinstance(item, dict):
            continue
        see.append({"t": _round(float(item.get("t") or 0)), "see": str(item.get("see") or "").strip()})
    if not see:
        for t, line in zip(cells, str(data.get("action") or "").split("；")):
            see.append({"t": t, "see": line.strip()})
    return {
        "adults": adults_n,
        "children": children_n,
        "mouth": data.get("mouth") if isinstance(data.get("mouth"), list) else [],
        "has_text": bool(data.get("has_text")),
        "action": str(data.get("action") or "").strip(),
        "unsure": [str(x).strip() for x in (data.get("unsure") or []) if str(x).strip()],
        "cells": see,
        "raw": raw,
    }


def _ensure_frames(video: Path, dest_dir: Path, times: list[float], log: Path) -> dict[float, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[float, Path] = {}
    for t in times:
        dest = dest_dir / f"f{_cs(t)}.jpg"
        if not dest.is_file() or dest.stat().st_size < 1000:
            extract_frame_at(video, dest, t, log_path=log)
        out[t] = dest
    return out


def _tick_text(items: list[dict[str, Any]], t: float, step: float) -> str:
    bits: list[str] = []
    for item in items:
        s = float(item.get("t0") or 0)
        e = float(item.get("t1") or s)
        if s - 1e-6 <= t < e + step * 0.51 or (e <= s and abs(s - t) <= step * 0.51):
            text = str(item.get("text") or "").strip()
            if text and text not in bits:
                bits.append(text)
    return " ".join(bits)


def build_ticks(
    windows: list[dict[str, Any]],
    *,
    duration: float,
    step: float,
    dialogue: dict[str, Any],
) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = []
    t = 0.0
    speech = list(dialogue.get("speech") or [])
    on_screen = list(dialogue.get("on_screen") or [])
    while t < duration - 1e-6:
        tt = _round(t)
        host = None
        for win in windows:
            if float(win["start"]) - 1e-6 <= tt < float(win["end"]) - 1e-6:
                host = win
                break
        cell = None
        if host:
            for item in host.get("cells") or []:
                if abs(float(item.get("t") or 0) - tt) <= step / 2 + 0.01:
                    cell = item
                    break
        ticks.append(
            {
                "t": tt,
                "adults": host.get("adults") if host else None,
                "children": host.get("children") if host else None,
                "has_text": host.get("has_text") if host else None,
                "see": (cell or {}).get("see") or "",
                "action": (host or {}).get("action") or "",
                "ocr": _tick_text(on_screen, tt, step),
                "asr": _tick_text(speech, tt, step),
            }
        )
        t = _round(t + step)
    return {"step": step, "duration": duration, "ticks": ticks}


def run_beat_table(
    settings: Settings,
    *,
    video: Path,
    directory: Path,
    duration: float,
    dialogue: dict[str, Any],
    log_path: Path,
) -> dict[str, Any]:
    window = float(settings.default.get("beat_window") or WINDOW)
    hop = float(settings.default.get("beat_hop") or HOP)
    step = float(settings.default.get("beat_step") or STEP)
    cells_n = int(settings.default.get("beat_cells") or CELLS)
    height = int(settings.default.get("beat_cell_h") or CELL_H)
    scene_thr = float(settings.default.get("beat_scene") or SCENE)

    cuts_path = directory / "scene_cuts.json"
    if cuts_path.is_file():
        try:
            cuts = [float(x) for x in json.loads(cuts_path.read_text(encoding="utf-8")).get("cuts") or []]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            cuts = scene_cuts(video, threshold=scene_thr)
            atomic_write_json(cuts_path, {"threshold": scene_thr, "cuts": cuts})
    else:
        cuts = scene_cuts(video, threshold=scene_thr)
        atomic_write_json(cuts_path, {"threshold": scene_thr, "cuts": cuts})

    starts = window_starts(duration, cuts, hop=hop)
    spans = [(s, min(_round(s + window), _round(duration))) for s in starts]
    needed: list[float] = []
    for start, end in spans:
        needed.extend(cell_times(start, end, step=step, cells=cells_n))
    needed = sorted(set(needed))
    frames = _ensure_frames(video, directory / "beats" / "frames", needed, log_path)

    beats_path = directory / "beats.json"
    existing = {}
    if beats_path.is_file():
        try:
            existing = json.loads(beats_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    old_rows = list(existing.get("windows") or [])
    by_span = {
        (_round(float(w.get("start") or 0)), _round(float(w.get("end") or 0))): w
        for w in old_rows
    }
    rows: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None

    def snapshot() -> dict[str, Any]:
        return {
            "window": window,
            "hop": hop,
            "step": step,
            "cells": cells_n,
            "cell_h": height,
            "duration": duration,
            "scene_threshold": scene_thr,
            "windows": rows,
        }

    for start, end in spans:
        key = (_round(start), _round(end))
        found = by_span.get(key)
        times = cell_times(start, end, step=step, cells=cells_n)
        if found and not found.get("error"):
            rows.append(found)
            prev = found
            continue
        if found and found.get("error") and found.get("raw"):
            # 旧 raw 能重新解析出来就省一次 VL；解析不了说明那次输出本身是坏的
            # （多半复读到截断），必须重新问一次，不能拿坏的 raw 一直顶着。
            try:
                parsed = _parse_beat(str(found["raw"]), times)
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None:
                parsed.pop("raw", None)
                found = {**found, **parsed}
                found.pop("error", None)
                found["must_open"] = flag_window(found, prev=prev, cuts=cuts, dialogue=dialogue)
                rows.append(found)
                prev = found
                continue
        cell_paths = [frames[t] for t in times if t in frames]
        dest = directory / "beats" / "strips" / f"w{_cs(start)}-{_cs(end)}.jpg"
        strip = dest if dest.is_file() and dest.stat().st_size > 1000 else make_strip(cell_paths, dest, height=height)
        win: dict[str, Any] = {
            "start": start,
            "end": end,
            "strip": str(strip.relative_to(directory)).replace("\\", "/") if strip else "",
            "cell_times": times,
        }
        if strip is None:
            win["error"] = "横条失败"
        else:
            prompt = BEAT_PROMPT.format(
                n=len(times),
                step=step,
                start=start,
                end=end,
                cell_list=" ".join(f"{t:.2f}" for t in times),
            )
            base = int(settings.default.get("vl_beats_max_tokens") or 1800)
            for tokens in (base, base * 2):
                try:
                    raw = analyze_image(settings, strip, prompt, max_new_tokens=tokens)
                    win["raw"] = raw
                    parsed = _parse_beat(raw, times)
                    parsed.pop("raw", None)
                    win.update(parsed)
                    win.pop("error", None)
                    break
                except VLError as exc:
                    win["error"] = str(exc)[:300]
                    if "out of memory" in str(exc).lower() or "oom" in str(exc).lower():
                        unload_vl()
                    break
                except (ValueError, TypeError) as exc:
                    # 多半是输出被 token 上限截断，加大额度再问一次
                    win["error"] = str(exc)[:300]
        win["must_open"] = flag_window(win, prev=prev, cuts=cuts, dialogue=dialogue)
        rows.append(win)
        prev = win
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"beat {start:.2f}-{end:.2f} adults={win.get('adults')} children={win.get('children')} "
                f"err={win.get('error') or ''} flags={','.join(win.get('must_open') or [])}\n"
            )
        atomic_write_json(beats_path, snapshot())

    doc = snapshot()
    atomic_write_json(beats_path, doc)
    ticks = build_ticks(rows, duration=duration, step=step, dialogue=dialogue)
    atomic_write_json(directory / "ticks.json", ticks)
    return doc
