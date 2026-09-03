from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from vrs.llmclient import LLMError, generate_text, llm_label, unload_llm
from vrs.lock import atomic_write_json
from vrs.settings import Settings
from vrs.textjson import parse_json_payload

GAP = 0.35
TITLE_REGIONS = {"title", "top", "upper"}
DROP_KEYS = {"past", "present", "clips", "clip", "clips_list", "summary"}
REASON_WEIGHT = {
    "片尾无对白": 3,
    "片尾硬切": 4,
    "人物组变化": 2,
    "画面硬切": 2,
    "解释性标题": 2,
    "对白空档": 1,
}
SHORTLIST_MERGE = 0.8
SNAP = 1.5
SHORTLIST_MAX = 40
STRONG_REASONS = {"解释性标题", "对白空档"}
# 事件边界只认这些：人物组变化常常只是同一人换装/长大，是段内两拍，不是新故事
ANCHOR_REASONS = {"画面硬切", "片尾硬切", "解释性标题", "片尾无对白"}
MIN_EVENT = 2.0
_ENDCARD_SEE = re.compile(r"字卡|黑底|书法|标题|白字|片尾")

PASS_A_PROMPT = """\
你在把一份拍表拆成事件段。默认只拆不并，不要问人。

输入是通用结构：hop 窗拍表、对白条、画面字条、规则候选切点。
不要假设这是哪一部作品。不要举例子。不要用「曾经 / 后来 / 过去 / 现在」当段标签或分类。不要为了凑段数去切，也不要为了少段而去并。

## 故事边界（必须拆成两个事件）
- 解释性标题：画面上方/标题区换了一块新的说明字（戏剧核换了）
- 对白分组：口播话题换了，且落在对白空档
- 两类及以上强信号同时出现的刀

## 段内两拍（不要拆成两个事件）
- 人物组变化单独出现：常常是同一人换装、长大、换景对照，不是另一段故事
- 画面硬切、换景：同一件事里的前后两拍。规则层稍后会按硬切拆成两条生成片，你不必在事件层切开
- 不要切出短于 2 秒的事件

## 硬约束
- 一条对白不许跨故事边界。切点必须落在对白空档（两条对白之间的缝，或片头/片尾无对白处）。
- 事件必须从 0 秒铺到片长，中间不要空洞。
- 对白结束后若剩下字卡/黑底说明，单独切一条 kind=endcard。有的片没有片尾：不要硬切一段空的 endcard。
- 相邻事件默认还是同一拨人。只有换成另一拨人时把 cast_reset 设为 true（第一条必须是 false）。
- 不要输出 clip 列表，不要标 past/present。

## 候选切点（短名单）
下面是规则层筛过的强候选。解释性标题换块的刀必须保留为事件边界。
人物组变化、画面硬切单独出现时，不要当成故事边界。
除了 0 和片长，每个 t0/t1 都必须**逐字**是上面某个候选的秒数，不许四舍五入、不许自己算一个附近的值。

{candidates}

## 拍表（每个 hop 窗一行）
{beats}

## 对白
{speech}

## 画面字（含区域）
{on_screen}

片长 {duration:.2f} 秒。最后一句对白结束于 {last_speech:.2f}s。

相邻两个候选只隔一两秒时，只能留一个：留信号更强的那个，另一个当同一刀的抖动丢掉。

只输出一个 JSON 对象，不要 markdown，不要任何解释：
{{"events":[{{"id":"e01","t0":0.0,"t1":12.3,"kind":"story","cast_reset":false}}]}}
kind 只能是 story 或 endcard。不要写 summary，不要加别的字段。
"""


def _round(t: float) -> float:
    return round(float(t), 2)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - 1e-6 and b0 < a1 - 1e-6


def _norm_title(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "")


def _same_title(a: str, b: str) -> bool:
    """OCR 每帧抖字，只有真的换了一句才算换标题。"""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def _title_groups(titles: list[dict[str, Any]]) -> list[tuple[float, list[str]]]:
    """同一时刻常有多行标题，按时间点归组后再比，避免行间交替被当成换块。"""
    bucket: dict[float, list[str]] = {}
    for item in sorted(titles, key=lambda x: float(x.get("t0") or 0)):
        t = _round(float(item.get("t0") or 0))
        text = _norm_title(str(item.get("text") or ""))
        if text and text not in bucket.setdefault(t, []):
            bucket[t].append(text)
    return [(t, bucket[t]) for t in sorted(bucket) if bucket[t]]


def _same_title_block(cur: list[str], prev: list[str]) -> bool:
    if not cur or not prev:
        return False
    hit = sum(1 for line in cur if any(_same_title(line, old) for old in prev))
    return hit * 2 >= len(cur)


def candidate_cuts(
    *,
    duration: float,
    scene: list[float],
    windows: list[dict[str, Any]],
    dialogue: dict[str, Any],
) -> list[dict[str, Any]]:
    bucket: dict[float, set[str]] = {}

    def add(t: float, reason: str) -> None:
        if t <= 0.15 or t >= duration - 0.15:
            return
        key = _round(t)
        bucket.setdefault(key, set()).add(reason)

    speech_all = sorted(dialogue.get("speech") or [], key=lambda s: float(s.get("t0") or 0))
    tail_from = max((float(s.get("t1") or 0) for s in speech_all), default=0.0)
    for cut in scene:
        t = float(cut)
        add(t, "片尾硬切" if tail_from > 0.2 and t > tail_from + 0.5 else "画面硬切")

    prev = None
    for win in windows:
        if prev is not None and win.get("adults") is not None and prev.get("adults") is not None:
            a = int(win.get("adults") or 0) + int(win.get("children") or 0)
            b = int(prev.get("adults") or 0) + int(prev.get("children") or 0)
            if a != b:
                add(float(win["start"]), "人物组变化")
        prev = win

    speech = sorted(dialogue.get("speech") or [], key=lambda s: float(s.get("t0") or 0))
    for i, cur in enumerate(speech):
        if i == 0:
            continue
        prev_e = float(speech[i - 1].get("t1") or 0)
        cur_s = float(cur.get("t0") or 0)
        if cur_s - prev_e >= GAP:
            add(_round((prev_e + cur_s) / 2), "对白空档")

    titles = [
        item
        for item in dialogue.get("on_screen") or []
        if str(item.get("region") or "") in TITLE_REGIONS and str(item.get("text") or "").strip()
    ]
    prev_lines: list[str] = []
    for t, lines in _title_groups(titles):
        if prev_lines and _same_title_block(lines, prev_lines):
            prev_lines = lines
            continue
        if prev_lines:
            add(t, "解释性标题")
        prev_lines = lines

    last_speech = max((float(s.get("t1") or 0) for s in speech), default=0.0)
    if duration - last_speech >= 0.4 and last_speech > 0.2:
        add(_round(last_speech), "片尾无对白")

    out = [{"t": t, "reasons": sorted(reasons)} for t, reasons in sorted(bucket.items())]
    return out


def cut_score(reasons: list[str]) -> int:
    return sum(REASON_WEIGHT.get(r, 1) for r in set(reasons))


def strong_signal_count(reasons: list[str]) -> int:
    return len(set(reasons) & STRONG_REASONS)


def shortlist_cuts(
    cands: list[dict[str, Any]],
    *,
    limit: int = SHORTLIST_MAX,
    merge: float = SHORTLIST_MERGE,
) -> list[dict[str, Any]]:
    """把密集候选并成强候选短名单：近邻合并，再按信号强度取前 N 条。

    连刷的同一块标题、同一次硬切的多帧命中，都会塌成一条。
    """
    groups: list[dict[str, Any]] = []
    for cand in sorted(cands, key=lambda c: float(c["t"])):
        t = float(cand["t"])
        reasons = sorted(set(cand.get("reasons") or []))
        if groups and t - float(groups[-1]["members"][0]["t"]) <= merge:
            groups[-1]["members"].append({"t": t, "reasons": reasons})
            continue
        groups.append({"members": [{"t": t, "reasons": reasons}]})
    for g in groups:
        members = g.pop("members")
        # 同一个边界常被拆成几个时间点，取信号最强的那个当代表
        best = max(members, key=lambda m: (cut_score(m["reasons"]), "画面硬切" in m["reasons"], -m["t"]))
        g["t"] = best["t"]
        g["reasons"] = sorted({r for m in members for r in m["reasons"]})
        g["score"] = cut_score(g["reasons"])

    def _keep(g: dict[str, Any]) -> bool:
        reasons = set(g.get("reasons") or [])
        if "解释性标题" in reasons or "画面硬切" in reasons or "片尾硬切" in reasons:
            return True
        return int(g.get("score") or 0) >= 3

    picked = [g for g in groups if _keep(g)]
    must = {
        g["t"]
        for g in picked
        if set(g.get("reasons") or []) & {"解释性标题", "画面硬切", "片尾硬切", "片尾无对白"}
    }
    picked.sort(key=lambda g: (-int(g["score"]), float(g["t"])))
    kept = [g for g in picked if g["t"] in must]
    extra = [g for g in picked if g["t"] not in must]
    room = max(0, limit - len(kept))
    kept.extend(extra[:room])
    return sorted(kept, key=lambda g: float(g["t"]))


def _anchor_cuts(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件边界只吸附画面硬切/标题/片尾，不吸附单独的人物组变化。"""
    out: list[dict[str, Any]] = []
    for cand in cands:
        reasons = set(cand.get("reasons") or [])
        if reasons & ANCHOR_REASONS:
            item = dict(cand)
            item["score"] = cut_score(list(item.get("reasons") or []))
            out.append(item)
    return out


def _fmt_beats(windows: list[dict[str, Any]]) -> str:
    lines = []
    for win in windows:
        people = f"{win.get('adults')}成人 {win.get('children')}孩子"
        flags = ",".join(win.get("must_open") or []) or "无"
        err = f" ERR={win.get('error')}" if win.get("error") else ""
        action = str(win.get("action") or "").replace("\n", " ")
        lines.append(
            f"{float(win['start']):.2f}-{float(win['end']):.2f}s  {people}  有字={bool(win.get('has_text'))}  "
            f"开图={flags}{err}  {action}"
        )
    return "\n".join(lines) or "（无拍表）"


def _fmt_span(items: list[dict[str, Any]], *, extra: str | None = None) -> str:
    lines = []
    for item in items:
        t0 = float(item.get("t0") or 0)
        t1 = float(item.get("t1") or t0)
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        suffix = ""
        if extra:
            suffix = f"  {item.get(extra)}"
        lines.append(f"{t0:.2f}-{t1:.2f}s  {text}{suffix}")
    return "\n".join(lines) or "（无）"


def _clean_event(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    try:
        t0 = _round(float(item.get("t0")))
        t1 = _round(float(item.get("t1")))
    except (TypeError, ValueError):
        return None
    if t1 <= t0:
        return None
    kind = str(item.get("kind") or "story").strip().lower()
    if kind not in {"story", "endcard"}:
        kind = "story"
    reset = item.get("cast_reset")
    if isinstance(reset, str):
        reset = reset.strip().lower() in {"1", "true", "yes", "y"}
    summary = str(item.get("summary") or "").strip()
    out = {
        "id": str(item.get("id") or f"e{index:02d}").strip() or f"e{index:02d}",
        "t0": t0,
        "t1": t1,
        "kind": kind,
        "cast_reset": bool(reset),
    }
    if summary:
        out["summary"] = summary
    for key in DROP_KEYS:
        out.pop(key, None)
    return out


def snap_to_shortlist(
    events: list[dict[str, Any]],
    short: list[dict[str, Any]],
    *,
    tol: float = SNAP,
) -> list[dict[str, Any]]:
    """边界必须落在规则认过的强候选上：附近有就吸附，没有就把这一刀撤掉。"""
    if not short or len(events) < 2:
        return events
    anchors = [float(c["t"]) for c in short]
    out = [events[0]]
    for ev in events[1:]:
        cut = float(ev["t0"])
        floor = float(out[-1]["t0"])
        inside = [a for a in anchors if abs(a - cut) <= tol and a > floor]
        if not inside:
            # 落在人物组抖动上的刀撤掉，并回上一段
            out[-1]["t1"] = ev["t1"]
            continue
        near = min(inside, key=lambda a: abs(a - cut))
        ev = {**ev, "t0": _round(near)}
        out[-1]["t1"] = _round(near)
        out.append(ev)
    return out


def _repair_dialogue_spans(
    events: list[dict[str, Any]],
    speech: list[dict[str, Any]],
    scene_times: list[float] | None = None,
) -> list[dict[str, Any]]:
    locked = [float(t) for t in (scene_times or [])]
    events = sorted(events, key=lambda e: float(e["t0"]))
    for i in range(1, len(events)):
        cut = float(events[i]["t0"])
        if any(abs(cut - s) <= 0.2 for s in locked):
            continue
        for sp in speech:
            s0 = float(sp.get("t0") or 0)
            s1 = float(sp.get("t1") or s0)
            if s0 + 0.02 < cut < s1 - 0.02:
                cut = s0 if (cut - s0) <= (s1 - cut) else s1
                events[i]["t0"] = _round(cut)
                events[i - 1]["t1"] = _round(cut)
    merged: list[dict[str, Any]] = []
    for ev in events:
        t0, t1 = float(ev["t0"]), float(ev["t1"])
        if t1 - t0 < 0.2:
            if merged:
                merged[-1]["t1"] = max(float(merged[-1]["t1"]), t1)
            continue
        if merged and abs(float(merged[-1]["t1"]) - t0) < 0.05:
            t0 = float(merged[-1]["t1"])
            ev["t0"] = _round(t0)
        merged.append(ev)
    return merged


def _cover(events: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    if not events:
        return [{"id": "e01", "t0": 0.0, "t1": _round(duration), "kind": "story", "cast_reset": False}]
    events[0]["t0"] = 0.0
    events[-1]["t1"] = _round(duration)
    for i in range(1, len(events)):
        events[i]["t0"] = _round(float(events[i - 1]["t1"]))
        if float(events[i]["t1"]) <= float(events[i]["t0"]):
            events[i]["t1"] = _round(min(duration, float(events[i]["t0"]) + 0.5))
    events[-1]["t1"] = _round(duration)
    for i, ev in enumerate(events, start=1):
        ev["id"] = f"e{i:02d}"
        ev["cast_reset"] = bool(ev.get("cast_reset")) if i > 1 else False
        ev["kind"] = str(ev.get("kind") or "story")
    return events


def _near_cut(short: list[dict[str, Any]], t: float, reason: str, *, tol: float = SNAP) -> bool:
    return any(
        abs(float(c["t"]) - t) <= tol and reason in (c.get("reasons") or [])
        for c in short
    )


def _force_strong_splits(
    events: list[dict[str, Any]],
    short: list[dict[str, Any]],
    duration: float,
) -> list[dict[str, Any]]:
    """解释性标题换块被模型并掉了也要拆回来。人物组变化不够当故事边界。"""
    cuts = [
        _round(float(c["t"]))
        for c in short
        if 0.2 < float(c["t"]) < duration - 0.2
        and "解释性标题" in (c.get("reasons") or [])
    ]
    if not cuts:
        return events
    min_piece = MIN_EVENT
    out: list[dict[str, Any]] = []
    for ev in events:
        t0, t1 = float(ev["t0"]), float(ev["t1"])
        inside = [c for c in cuts if t0 + min_piece < c < t1 - min_piece]
        points = [t0, *inside, t1]
        for a, b in zip(points, points[1:]):
            piece = dict(ev)
            piece["t0"] = _round(a)
            piece["t1"] = _round(b)
            piece["kind"] = str(ev.get("kind") or "story")
            piece["cast_reset"] = bool(ev.get("cast_reset")) and abs(a - t0) < 0.02
            out.append(piece)
    return out


def _merge_tiny(events: list[dict[str, Any]], *, min_span: float = MIN_EVENT) -> list[dict[str, Any]]:
    """短于 2s 的残片并进邻段，避免 1s 源片被 pad 成 4.5s 空演。"""
    events = sorted(events, key=lambda e: float(e["t0"]))
    out: list[dict[str, Any]] = []
    for ev in events:
        if str(ev.get("kind") or "story") == "endcard":
            out.append(ev)
            continue
        span = float(ev["t1"]) - float(ev["t0"])
        if out and str(out[-1].get("kind") or "story") != "endcard":
            prev_span = float(out[-1]["t1"]) - float(out[-1]["t0"])
            if span < min_span:
                out[-1]["t1"] = ev["t1"]
                continue
            if prev_span < min_span:
                merged = dict(ev)
                merged["t0"] = out[-1]["t0"]
                merged["cast_reset"] = bool(out[-1].get("cast_reset"))
                out[-1] = merged
                continue
        out.append(ev)
    return out


def _apply_cast_reset(events: list[dict[str, Any]], short: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, ev in enumerate(events):
        if i == 0:
            ev["cast_reset"] = False
            continue
        forced = _near_cut(short, float(ev["t0"]), "人物组变化")
        ev["cast_reset"] = bool(ev.get("cast_reset")) or forced
    return events


def _tail_looks_like_endcard(
    t0: float,
    t1: float,
    windows: list[dict[str, Any]],
    on_screen: list[dict[str, Any]],
) -> bool:
    title = False
    for item in on_screen:
        s0, s1 = float(item.get("t0") or 0), float(item.get("t1") or item.get("t0") or 0)
        if s1 <= t0 or s0 >= t1:
            continue
        region = str(item.get("region") or "")
        text = str(item.get("text") or "").strip()
        if text and region in TITLE_REGIONS:
            title = True
    counted_zero = False
    for win in windows:
        w0, w1 = float(win.get("start") or 0), float(win.get("end") or 0)
        if w1 <= t0 or w0 >= t1:
            continue
        see = str(win.get("action") or win.get("seen") or "")
        if _ENDCARD_SEE.search(see):
            title = True
        adults, children = win.get("adults"), win.get("children")
        if adults is None and children is None:
            continue
        n = int(adults or 0) + int(children or 0)
        if n == 0:
            counted_zero = True
        else:
            return title
    return title or counted_zero


def _split_endcard(
    events: list[dict[str, Any]],
    speech: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    on_screen: list[dict[str, Any]],
    duration: float,
) -> list[dict[str, Any]]:
    """对白结束后的字卡单独切成 endcard，不把前面的故事改成片尾。"""
    last_speech = max((float(s.get("t1") or 0) for s in speech), default=0.0)
    for ev in events:
        if ev.get("kind") == "endcard" and _overlap(float(ev["t0"]), float(ev["t1"]), 0, last_speech):
            ev["kind"] = "story"
    if last_speech <= 0.2 or duration - last_speech < 0.3:
        return events
    cut = _round(last_speech)
    if cut >= duration - 0.15:
        return events
    if not _tail_looks_like_endcard(cut, duration, windows, on_screen):
        return events
    out: list[dict[str, Any]] = []
    for ev in events:
        t0, t1 = float(ev["t0"]), float(ev["t1"])
        if t1 <= cut + 0.02:
            ev["kind"] = "story" if ev.get("kind") == "endcard" else ev.get("kind") or "story"
            out.append(ev)
            continue
        if t0 >= cut - 0.02:
            ev["kind"] = "endcard"
            ev["cast_reset"] = False
            out.append(ev)
            continue
        head = dict(ev)
        head["t1"] = cut
        head["kind"] = "story"
        tail = dict(ev)
        tail["t0"] = cut
        tail["t1"] = t1
        tail["kind"] = "endcard"
        tail["cast_reset"] = False
        out.append(head)
        out.append(tail)
    return out


def _fmt_stats(stats: dict[str, Any]) -> str:
    if not stats:
        return ""
    ms = stats.get("ms")
    bits = [f"model={stats.get('model') or '?'}"]
    if ms:
        bits.append(f"{float(ms) / 1000:.1f}s")
    bits.append(f"tok={stats.get('tok_in') or 0}/{stats.get('tok_out') or 0}")
    if stats.get("run"):
        bits.append(f"run={stats['run']}")
    return " ".join(bits)


def _ask_llm(settings: Settings, prompt: str, directory: Path) -> Any:
    """问一次文本 LLM，并把提示词与原文落盘，失败时也留证据。"""
    raw_path = directory / "logs" / "passa.raw.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(1, 3):
        raw = ""
        stats: dict[str, Any] = {}
        try:
            raw = generate_text(settings, prompt, stats=stats)
            payload = parse_json_payload(raw, require="events")
        except (LLMError, ValueError) as exc:
            last = exc
            payload = None
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(f"===== attempt {attempt} prompt_chars={len(prompt)} =====\n")
            handle.write(prompt if attempt == 1 else "（同上）")
            handle.write(f"\n----- raw chars={len(raw)} {_fmt_stats(stats)} -----\n{raw}\n")
            handle.write(f"----- parsed={payload is not None} err={last} -----\n\n")
        if payload is not None:
            return payload
    raise LLMError(f"Pass A 无法解析 LLM 输出：{last}")


def finalize_events(
    raw_events: list[dict[str, Any]],
    *,
    short: list[dict[str, Any]],
    cands: list[dict[str, Any]],
    speech: list[dict[str, Any]],
    windows: list[dict[str, Any]],
    on_screen: list[dict[str, Any]],
    duration: float,
) -> list[dict[str, Any]]:
    events = []
    for i, item in enumerate(raw_events, start=1):
        if isinstance(item, dict):
            cleaned = _clean_event(item, i)
            if cleaned:
                events.append(cleaned)
    anchors = _anchor_cuts(cands)
    scene_times = [
        float(c["t"])
        for c in cands
        if {"画面硬切", "片尾硬切"} & set(c.get("reasons") or [])
    ]
    events = snap_to_shortlist(events, anchors)
    events = _repair_dialogue_spans(events, speech, scene_times)
    events = _cover(events, duration)
    events = _force_strong_splits(events, short, duration)
    events = _merge_tiny(events)
    events = _repair_dialogue_spans(events, speech, scene_times)
    events = _cover(events, duration)
    events = _apply_cast_reset(events, short)
    events = _split_endcard(events, speech, windows, on_screen, duration)
    events = _merge_tiny(events)
    events = _cover(events, duration)
    return events


def run_pass_a(
    settings: Settings,
    *,
    directory: Path,
    duration: float,
    beats: dict[str, Any],
    dialogue: dict[str, Any],
    scene: list[float],
) -> dict[str, Any]:
    windows = list(beats.get("windows") or [])
    speech = list(dialogue.get("speech") or [])
    cands = candidate_cuts(duration=duration, scene=scene, windows=windows, dialogue=dialogue)
    short = shortlist_cuts(cands)
    cand_txt = "\n".join(
        f"{c['t']:.2f}s  强度{c['score']}  {', '.join(c['reasons'])}" for c in short
    ) or "（无）"
    last_speech = max((float(s.get("t1") or 0) for s in speech), default=0.0)
    prompt = PASS_A_PROMPT.format(
        candidates=cand_txt,
        beats=_fmt_beats(windows),
        speech=_fmt_span(speech),
        on_screen=_fmt_span(list(dialogue.get("on_screen") or []), extra="region"),
        duration=duration,
        last_speech=last_speech,
    )
    payload = _ask_llm(settings, prompt, directory)
    if isinstance(payload, list):
        raw_events = payload
    elif isinstance(payload, dict):
        raw_events = payload.get("events") or []
    else:
        raw_events = []
    events = finalize_events(
        raw_events,
        short=short,
        cands=cands,
        speech=speech,
        windows=windows,
        on_screen=list(dialogue.get("on_screen") or []),
        duration=duration,
    )
    doc = {
        "duration": duration,
        "events": events,
        "shortlist": short,
        "candidates": cands,
        "llm": llm_label(settings),
    }
    atomic_write_json(directory / "events.json", doc)
    unload_llm()
    return doc
