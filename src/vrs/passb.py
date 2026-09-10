"""Pass B：给每个 H3 片段写英文提示词。

一段一次调用，按顺序走：先写的段把说话人外观锁定下来，后面的段必须逐字复用，
这样同一个人不会在第三段换脸。模型只回 JSON，官方格式的正文由代码拼——
首行的 S.SS、[Shot N] 的时间戳都由构造保证，不指望模型格式写得准。

画面字一律不进提示词，也不要写 subtitle/caption/Avoid：H3 会当成要烧字幕。
字幕留到成片阶段再叠 ASS。配乐默认 N/A，不要复刻 BGM。
"""

from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from vrs.h3grid import PATH_KEYFRAMES, PATH_LOCK_ACROSS, normalize_generate_path
from vrs.llmclient import LLMError, generate_text
from vrs.promptcheck import MIN_SHOT, check_clip, check_header, check_mode, iter_clip_speech
from vrs.settings import Settings
from vrs.textjson import parse_json_payload

# 横条是 7680x360，发给视觉模型会被内部缩放到每格百来像素，外观细节全丢。
# 改发 1920x1080 的单帧：一张 100KB，细节留得住，上传也快得多。
MAX_FRAMES = 8
MAX_TRIES = 3
WRITER_REV = 8
# 金标 event_chain：硬切落在区间末尾，最后 0.2s 的格常是下一条第一帧。
EDGE = 0.20

SCHEMA = """{
  "zh": {
    "event_chain": "事件链：看见→靠近→接触→结果，一句一环，中文，点名物件和空间，不要收成结果态",
    "beats": ["a.aa-b.bb 开口前/说的时候/说完保持：视线/眉眼嘴/手与道具/身体/衣物怎么动，不要只写情绪名"],
    "amplitude": "档位（微表情/小幅度/中等/大幅度）；上限；情绪曲线",
    "scene": "中文场景：地点、时段、光线、色温、地面、主要陈设，和 scene_lock 对应",
    "soundscape": "中文环境音：底噪、衣物、呼吸、物体声，不含配乐，和 overall_soundscape 对应",
    "note": "拿不准的地方写在这里，没有就空字符串"
  },
  "style": "live-action photorealistic",
  "scene_lock": "one English paragraph: place, time of day, light, color temperature, floor, key furniture",
  "speakers": [
    {
      "id": "S1",
      "lock": "Identity lock paragraph: ethnicity, age, face, hair, every clothing layer, handheld props",
      "voice": "Voice lock: age-gender, language, pitch, timbre, rate baseline",
      "zh": "中文对照"
    }
  ],
  "shots": [
    {"index": 1, "at": null, "text": "English shot body, no [Shot 1] prefix, no timestamp. Must include Identity lock, Voice lock, Scene lock, then spatial action."},
    {"index": 2, "at": 3.5, "text": "English shot body, no [Shot 2] prefix, no timestamp"}
  ],
  "overall_soundscape": "1-4 English sentences of room tone, cloth, breath, object sounds. No music.",
  "non_diegetic_music": "N/A"
}"""


class PassBError(RuntimeError):
    pass


def _fmt(seconds: float) -> str:
    return f"{float(seconds):.2f}"


def _stamp(seconds: float) -> str:
    """官方规格 4.2 的切点写法：At 00:03.500。"""
    total = float(seconds)
    return f"{int(total // 60):02d}:{total % 60:06.3f}"


def _local(src: float, clip: dict[str, Any]) -> float:
    """源片时间换成本条内部时间。网格对齐让时长有 ±0.15s 漂移，按比例缩。"""
    span = float(clip["source_seconds"]) or 1.0
    ratio = float(clip["h3_seconds"]) / span
    return max(0.0, min(float(clip["h3_seconds"]), (float(src) - float(clip["t0"])) * ratio))


def _dedupe(cells: list[dict[str, Any]], *, lookback: int = 2, reach: float = 1.5) -> list[tuple[float, float, str]]:
    """看见同一件事就并成一段。

    8B 常在相邻格之间来回给两种说法（A/B/A/B），只比上一条会刷出一屏。
    所以往回看几条：最近出现过的同一句直接延长它的区间，不新开一行。
    """
    out: list[list[Any]] = []
    for cell in cells:
        see = str(cell.get("see") or "").strip()
        t = float(cell.get("t") or 0)
        if not see:
            continue
        hit = next(
            (row for row in reversed(out[-lookback:]) if row[2] == see and t - row[1] <= reach),
            None,
        )
        if hit is not None:
            hit[1] = t
        else:
            out.append([t, t, see])
    return [(a, b, see) for a, b, see in out]


def _one_per_cell(
    cells: list[dict[str, Any]], step: float, *, t0: float | None = None, t1: float | None = None
) -> list[dict[str, Any]]:
    """拍表窗是重叠的，同一时刻会被好几个窗各描述一遍，交错起来像两条线在闪。

    量化到抽帧网格，一格只留一条（窗按起点排序，先覆盖到的赢）。
    不能把切点 5.57 收成 5.50：那是上一段末帧，会把过去的人和景写进本条开场。
    最后 EDGE 秒常是下一段第一帧，丢掉。
    """
    kept: dict[int, dict[str, Any]] = {}
    hi = None if t1 is None else t1 - EDGE
    for cell in cells:
        t = float(cell.get("t") or 0)
        if t0 is not None and t < t0 - 1e-6:
            continue
        if hi is not None and t >= hi - 1e-6:
            continue
        slot = int(round(t / step))
        snapped = slot * step
        if t0 is not None and snapped < t0:
            snapped = t
            slot = int(round(t * 1000))
        if hi is not None and snapped >= hi:
            continue
        kept[slot] = {"t": snapped, "see": cell.get("see")}
    return [kept[k] for k in sorted(kept)]


def _thin_cuts(cuts: list[float], seconds: float, *, min_shot: float = 1.0) -> list[float]:
    """挨得太近的硬切是抖动误检，照着分镜会切出 0.07 秒的镜头。"""
    out: list[float] = []
    for c in sorted(cuts):
        if c < min_shot or c > seconds - min_shot:
            continue
        if out and c - out[-1] < min_shot:
            continue
        out.append(c)
    return out


def _spread(items: list[Any], limit: int) -> list[Any]:
    """要抽稀就均匀抽，取前 N 张会让片段后半段一张图都没有。"""
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[int(round(i * step))] for i in range(limit)]


def _cs_name(t: float) -> str:
    return f"f{int(round(float(t) * 100)):05d}.jpg"


def _frame_file(root: Path, t: float) -> Path:
    return root / "beats" / "frames" / _cs_name(t)


def _still_catalog(root: Path, lo: float, hi: float) -> list[tuple[float, Path]]:
    dest = root / "beats" / "frames"
    out: list[tuple[float, Path]] = []
    if not dest.is_dir():
        return out
    for path in dest.glob("f*.jpg"):
        stem = path.stem[1:]
        if not stem.isdigit():
            continue
        t = int(stem) / 100.0
        if lo - 1e-6 <= t <= hi + 1e-6 and path.is_file() and path.stat().st_size >= 1000:
            out.append((t, path))
    out.sort()
    return out


def _clip_stills(clip: dict[str, Any], root: Path, *, limit: int) -> list[tuple[float, Path]]:
    """只抽本条 [t0, t1-EDGE] 里的真帧。第 1 张必须是开场，最后一张必须还在本条。"""
    t0, t1 = float(clip["t0"]), float(clip["t1"])
    hi = max(t0, t1 - EDGE)
    timed = _still_catalog(root, t0, hi)
    if not timed:
        for t in (t0, (t0 + hi) / 2.0, hi):
            path = _frame_file(root, t)
            if path.is_file() and path.stat().st_size >= 1000:
                timed.append((t, path))
    return _spread(timed, limit)


def _clean_t(raw: str) -> float | None:
    num = re.sub(r"[^\d.]", "", str(raw).replace(",", "."))
    num = re.sub(r"\.{2,}", ".", num)
    try:
        return float(num) if num else None
    except ValueError:
        return None


_SEE_ITEM = re.compile(r'"t"\s*:\s*(?P<t>[^,]+?)\s*,\s*"see"\s*:\s*"(?P<see>[^"]*)"')


def _salvage_cells(win: dict[str, Any]) -> list[dict[str, Any]]:
    """拍表 JSON 经常把 t 写成「极4.0」，解析失败后 cells 是空的。从 raw 把 see 捞回来。"""
    out: list[dict[str, Any]] = []
    for item in win.get("cells") or []:
        if not isinstance(item, dict):
            continue
        see = str(item.get("see") or "").strip()
        if not see:
            continue
        out.append({"t": float(item.get("t") or 0), "see": see})
    if out:
        return out
    raw = str(win.get("raw") or "")
    for match in _SEE_ITEM.finditer(raw):
        t = _clean_t(match.group("t"))
        see = str(match.group("see") or "").strip()
        if t is None or not see:
            continue
        out.append({"t": t, "see": see})
    return out


def _salvage_field(win: dict[str, Any], key: str) -> str:
    value = str(win.get(key) or "").strip()
    if value:
        return value
    raw = str(win.get("raw") or "")
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', raw)
    return str(match.group(1) or "").strip() if match else ""


def _salvage_count(win: dict[str, Any], key: str) -> int | None:
    value = win.get(key)
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    raw = str(win.get("raw") or "")
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(\d+)', raw)
    return int(match.group(1)) if match else None


def _windows_for_clip(windows: list[dict[str, Any]], t0: float, t1: float) -> list[dict[str, Any]]:
    """只留落在本条里的窗。上一窗的尾巴、下一窗的开头都会把别人的人写进来。"""
    span = max(0.4, t1 - t0)
    min_ov = min(1.0, max(0.4, span * 0.45))
    scored: list[tuple[float, dict[str, Any]]] = []
    for win in windows:
        start = float(win.get("start") or 0)
        end = float(win.get("end") or 0)
        overlap = min(end, t1) - max(start, t0)
        if overlap <= 0.01:
            continue
        scored.append((overlap, win))

    def _in_story(win: dict[str, Any]) -> bool:
        start = float(win.get("start") or 0)
        end = float(win.get("end") or 0)
        if start < t0 - 0.2:
            return False
        if end > t1 + 0.2 and start > t0 + 0.4:
            return False
        return True

    kept = [win for overlap, win in scored if overlap >= min_ov and _in_story(win)]
    if kept:
        return kept
    inside = [win for overlap, win in scored if _in_story(win)]
    if inside:
        return inside
    scored.sort(key=lambda item: item[0], reverse=True)
    return [scored[0][1]] if scored else []


def clip_facts(
    clip: dict[str, Any],
    beats: dict[str, Any],
    dialogue: dict[str, Any],
    cuts: list[float],
    *,
    root: Path,
    clips: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把一条片段要用到的原片事实收齐，全部换算成本条内部时间。"""
    t0, t1 = float(clip["t0"]), float(clip["t1"])
    step = float(beats.get("step") or 0.25)
    covering = _windows_for_clip(list(beats.get("windows") or []), t0, t1)
    cells: list[dict[str, Any]] = []
    actions: list[str] = []
    adults: list[int] = []
    children: list[int] = []
    mouths: list[str] = []
    for win in covering:
        start = float(win.get("start") or 0)
        end = float(win.get("end") or 0)
        extends = end > t1 + 0.25
        action = _salvage_field(win, "action")
        if action:
            actions.append(f"{start:.2f}-{end:.2f}s {action}")
        n_ad = _salvage_count(win, "adults")
        n_ch = _salvage_count(win, "children")
        if n_ad is not None:
            adults.append(n_ad)
        if n_ch is not None:
            children.append(n_ch)
        for mouth in win.get("mouth") or []:
            if isinstance(mouth, dict):
                mouths.append(
                    f"{mouth.get('who') or '?'} {mouth.get('state') or '?'} {mouth.get('cells') or ''}"
                )
        for cell in _salvage_cells(win):
            t = float(cell.get("t") or 0)
            if t < t0 - 0.01 or t >= t1 - EDGE:
                continue
            if extends and t >= t1 - 0.55:
                continue
            cells.append(cell)
    uniq = _one_per_cell(cells, step, t0=t0, t1=t1)

    speech = [
        {
            "a": _local(max(t0, float(s["t0"])), clip),
            "b": _local(min(t1, float(s["t1"])), clip),
            "text": str(s.get("text") or "").strip(),
            "emotion": str(((s.get("vocal_emotion") or {}).get("label")) or ""),
        }
        for s in iter_clip_speech(clip, dialogue, clips)
    ]
    vision = _dedupe(uniq)
    frames = _clip_stills(clip, root, limit=MAX_FRAMES)
    neighbors: list[dict[str, Any]] = []
    if clips:
        idx = next((i for i, c in enumerate(clips) if c.get("id") == clip.get("id")), None)
        if idx is not None:
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(clips):
                    peer = clips[j]
                    lines = [
                        str(s.get("text") or "").strip()
                        for s in iter_clip_speech(peer, dialogue, clips)
                        if str(s.get("text") or "").strip()
                    ]
                    if lines:
                        neighbors.append(
                            {
                                "id": peer["id"],
                                "t0": float(peer["t0"]),
                                "t1": float(peer["t1"]),
                                "lines": lines,
                            }
                        )
    return {
        "vision": vision,
        "speech": [s for s in speech if s["text"]],
        "cuts": _thin_cuts(
            [_local(c, clip) for c in cuts if t0 + 0.04 < c < t1 - 0.04],
            float(clip["h3_seconds"]),
        ),
        "frames": frames,
        "actions": actions,
        "adults": max(adults) if adults else None,
        "children": max(children) if children else None,
        "mouths": mouths,
        "neighbors": neighbors,
    }


def _instruction(path: str) -> str:
    if PATH_KEYFRAMES.get(path):
        return (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced."
        )
    return ""


def build_prompt(
    clip: dict[str, Any],
    facts: dict[str, Any],
    *,
    path: str,
    locks: dict[str, str],
    errors: list[str] | None = None,
) -> str:
    seconds = float(clip["h3_seconds"])
    vision = "\n".join(
        f"  {_fmt(_local(a, clip))}-{_fmt(_local(b, clip))}  {see}" for a, b, see in facts["vision"]
    ) or "  （这一段拍表格是空的，必须按附图写）"
    speech = "\n".join(
        f"  {_fmt(s['a'])}-{_fmt(s['b'])}  「{s['text']}」（语气 {s['emotion'] or '未标'}）"
        for s in facts["speech"]
    ) or "  （本段没有对白）"
    cuts = "、".join(_fmt(c) + "s" for c in facts["cuts"]) or "（自动检测没报，你看图自己判断）"
    n_fr = len(facts["frames"] or [])
    frame_lines = []
    for i, (t, _p) in enumerate(facts["frames"] or []):
        tag = "开场" if i == 0 else ("末帧" if i == n_fr - 1 else "本条内")
        frame_lines.append(
            f"  第 {i + 1} 张【{tag}】：本条内部 {_fmt(_local(t, clip))}s（源片 {_fmt(t)}s）"
        )
    frames = "\n".join(frame_lines) or "  （没有可用的帧）"
    t0, t1 = float(clip["t0"]), float(clip["t1"])
    bounds = (
        f"金标边界：第 1 张是源片 {_fmt(t0)}s，必须当本条开场；"
        f"最后一张是源片 {_fmt(max(t0, t1 - EDGE))}s，必须当本条末帧。"
        f"源片 {_fmt(t1)}s 起是下一段硬切，人和景一个字都不要写进来。"
        "不要写上一段的场景名。本段附图里没出现的人，一个字都不写（包括 is not on screen）。"
    )
    actions = "\n".join(f"  {line}" for line in (facts.get("actions") or [])) or "  （窗级动作链缺失，按附图补）"
    n_ad = facts.get("adults")
    n_ch = facts.get("children")
    if n_ad is None and n_ch is None:
        people = "  （拍表没写出人数，按附图数清楚：几个成人、几个孩子、谁在左谁在右）"
    else:
        people = f"  成人 {0 if n_ad is None else n_ad}，儿童 {0 if n_ch is None else n_ch}。空位写实物（空椅、空桌），不要点名否定。"
    mouths = "\n".join(f"  {m}" for m in (facts.get("mouths") or [])) or "  （未标）"
    locked = "\n".join(f"  {sid}：{lock}" for sid, lock in locks.items()) or "  （还没有锁定的人）"
    reset = (
        "本条是新的一拨角色：上面没有可沿用的人。S 号从本条重新起，不要沿用上一段的说话人。\n"
        if clip.get("cast_reset")
        else ""
    )
    if PATH_KEYFRAMES.get(path):
        mission = "你在给 MiniMax H3 写一条视频生成提示词。这是复刻原片，不是二创。细节必须写到能照着演。"
        head = (
            f"本条走 I2VA：源片 {_fmt(clip['t0'])}s 那一帧会作为首帧 <Picture 1> 喂给 H3。\n"
            "[Shot 1] 必须从这一帧的构图、人物外观、环境写起（写成「the ... shown in "
            "<Picture 1>」这类锚定），再往前发展。不要描述末帧。\n"
            "Identity lock / Voice lock / Scene lock 仍要写进正文，且外观按图写细，不要一句带过。"
        )
        frames_head = "附图（必须逐张看，外观、站位、道具、光线按图写，不要只照下面的文字摘要写）"
        lock_head = "已锁定的说话人外观（必须逐字复用，不许改写）"
        lock_rule = "说话人用 (S1)(S2)，首次出现要在 speakers.lock 写完整 Identity lock；已锁定的逐字复用"
    else:
        mission = (
            "你在给 MiniMax H3 写一条视频生成提示词。这是复刻原片的情节，不是摘要。"
            "人物五官不必长得和原片一样，但衣服、站位、道具、人数、动作链、光线必须按附图写细。"
        )
        head = (
            "本条走 T2VA：没有关键帧，整条时间线由文字构建。不要提 <Picture>。\n"
            "不要沿用上一段的外观锁。本条自己写三种锁，并写进每一镜正文：\n"
            "- Identity lock：族裔、年龄、脸、发型、每一层衣服、手里的东西\n"
            "- Voice lock：年龄段、性别、语言、音高、音色、语速基线\n"
            "- Scene lock：地点、时段、天气/室内光、色温、地面、主要陈设\n"
            "画面写成 live-action photorealistic, raw camera footage, empty corners, no logos。\n"
            "Widescreen 16:9 landscape, 1920x1080 horizontal cinema frame, not vertical 9:16, not a phone portrait crop。\n"
            "第一镜构图必须对上附图第 1 张（本条源片第一帧），不是对上结束时的状态。"
            "第 1 张是胸口特写：就胸口以上开场，衣服铺满底部。"
            "第 1 张能看见全身、障碍物、翻越：按图写能看见那个过程，不要收成落地之后或已经站稳的特写。\n"
            "人数用正面写法：Total cast in this clip: N people ...；空位写成空地面/空椅，不要点名否定、不要写 is not on screen。\n"
            "Identity lock 只写本条附图里入画的人。邻条换了装、换了场的人一个字都不写。\n"
            "宽景里的人没有说话任务时写成 lips pressed into a thin line, jaw clenched。\n"
            "黑板、海报、校徽写成 unreadably textured / blank shapes，不要点名真汉字或真徽章。\n"
            "空间写几何，不写意图：past (S2)'s nearer shoulder onto (S1)'s chest，不要写 points at him。\n"
            "不要把过程收成结果态。看见→靠近→接触→结果都要写进正文。"
            "禁止 already holding / already standing / 落地之后 当开场。\n"
            "表演必须按本条内部时间写成 From a.aa to b.bb 节拍，至少三截："
            "开口前静息 → 说的时候（口型和对白落在这一截）→ 说完保持到成片时长。"
            "默认微表情、小幅度；只有附图里真的在转圈/蹦跳/翻越时才加大动作。\n"
            "禁止 emotional / beautiful / expressive / cinematic。"
        )
        frames_head = "附图（必须逐张看。情节、场次、站位、道具、光线以图为准；拍表 cells 只是时间索引，常常过短或解析坏了）"
        lock_head = "本条人物外观（T2VA 不沿用上一段，按本条附图自己写细）"
        lock_rule = "说话人用 (S1)(S2)，本条自己写完整 Identity lock 和 Voice lock，不要写成一句年龄+衣服"
        reset = ""
        locked = "  （T2VA 不跨段锁脸）"

    pad = ""
    if clip.get("padded"):
        pad = (
            f"原片这一拍只有 {_fmt(float(clip.get('source_seconds') or 0))}s，"
            f"成片写成 {_fmt(seconds)}s。"
            "多出来的时间只用来演本条这一环还没演完的过程，或保持本条末帧的人和景做反应。"
            "不要新开一场，不要把下一幕拉进来，不要把结果态拉长到结束。\n"
        )

    retry = ""
    if errors:
        retry = "\n## 上一版没过机检，只改这些\n\n" + "\n".join(f"- {e}" for e in errors) + "\n"

    neighbor_block = "  （没有邻条对白）"
    if facts.get("neighbors"):
        rows = []
        for item in facts["neighbors"]:
            lines = " / ".join(item["lines"])
            rows.append(
                f"  {item['id']} 源片 {_fmt(item['t0'])}-{_fmt(item['t1'])}s：{lines}"
            )
        neighbor_block = "\n".join(rows)

    return f"""{mission}

{head}
{bounds}
{pad}
## 本条

片段 {clip["id"]}｜源片 {_fmt(clip["t0"])}-{_fmt(clip["t1"])}s｜**成片时长 {_fmt(seconds)}s**（{clip["h3_frames"]} 帧）
自动检测到的硬切（本条内部时间，只是参考，会漏）：{cuts}

## {frames_head}

{frames}

## 附图人数（拍表，可能不准，以图为准）

{people}

## 窗级动作链（看见→靠近→接触→结果）

{actions}

## 嘴型（画面上谁嘴在动 ≠ 谁在说那句台词）

{mouths}

## 8B 读出来的画面（本条内部时间；过短或空了就看图，不要照抄空话）

{vision}

## 对白原文（进 <d> 必须逐字照抄，一个字都不能改）

{speech}

## 邻条对白（禁止跟读，禁止写进本条任何 <d>）

{neighbor_block}

## {lock_head}

{reset}{locked}

## 规矩

- 全部英文，只有 <d> 里面能出现汉字。不要用英文引号包汉字
- **<d> 的标签只能是 `[Chinese]`**，写成 `<d>[Chinese] 原句</d>`。语气不写进标签，写在 <d> 外面
- 不许编对白、不许翻译对白、不许删对白。上面每一句都要落到某个镜头里
- 邻条那几句一个字都不要写进本条。本条只说「对白原文」里的句子
- **禁止**英文出现 subtitle、caption、burned-in、on-screen text、Chinese text overlay。写这些词会烧字幕
- 不要复述原片烧在画面上的字幕和标题
- {lock_rule}
- Identity lock 只列本条附图里真正入画的人。没入画的人一个字都不写，也不写 is not on screen
- [Shot 1] 正文开头必须出现 `Identity lock`、`Voice lock`、`Scene lock` 三句，后面才是动作
- [Shot 1] 开场必须对上附图第 1 张，不是本条结束态
- 英文动作用 `From a.aa to b.bb` 写出至少三截节拍：开口前 / 说的时候 / 说完保持，铺满 0 到 {_fmt(seconds)}s
- zh.beats 用本条内部时间，同样至少三截：开口前、说话时、说完
- 末帧只停在附图最后一张的人和景。下一段硬切的人和景不要写进来
- 附图若换了场或换了人：只采用和第 1 张同一场的；另一场是切点上的邻段，丢掉
- speakers.lock 只写外观，speakers.voice 写声音；scene_lock 单独成段
- 一条只演一个事件。看图发现换了人、换了故事，不要塞进本条
- 不要写上一段的场景名，写了会把上一段的人和景带回来
- 换场就分镜：本条内部换机位可以切；禁止切到上一段或下一段的人和景
- shots[0].at 必须是 null；后面的 at 严格递增且小于 {_fmt(seconds)}
- **每一镜至少 {MIN_SHOT:.1f}s**（含最后一镜）。切在 0.15s 的第二镜等于没有首镜，宁可少切一刀
- style 只写 `live-action photorealistic`，不要 cinematic / beautiful
- 镜头运动写成自然英语动作，带上运动类型，幅度和速度只在有意义时写；不动就写 holds a static shot
- overall_soundscape 只写环境音、动作音、非语言人声，不要重复对白，不要写配乐
- **non_diegetic_music 必须是 N/A**，不要复刻 BGM
- 描述铺满 0 到 {_fmt(seconds)}s，不要多也不要少
{retry}
## 只输出这个 JSON，不要围栏不要解释

{SCHEMA}
"""


def assemble_txt(doc: dict[str, Any], clip: dict[str, Any], path: str) -> str:
    """按官方 base-en.txt 的段序拼正文。时间戳和 S.SS 由这里保证，不经模型的手。"""
    style = str(doc.get("style") or "").strip().rstrip(".,;: ")
    parts: list[str] = []
    for shot in doc.get("shots") or []:
        body = str(shot.get("text") or "").strip()
        index = int(shot.get("index") or (len(parts) + 1))
        if shot.get("at") is None:
            parts.append(f"[Shot {index}] {style}, {body}")
        else:
            parts.append(f"[Shot {index}] At {_stamp(float(shot['at']))}, {body}")
    music = str(doc.get("non_diegetic_music") or "").strip() or "N/A"
    if music.upper() != "N/A":
        music = "N/A"
    lines = [
        f"integrated_multimodal_description: {' '.join(parts)}",
        f"overall_soundscape: {str(doc.get('overall_soundscape') or '').strip()}",
        f"non_diegetic_music: {music}",
    ]
    head = _instruction(path)
    return ("\n\n".join([head, *lines]) if head else "\n\n".join(lines)) + "\n"


def assemble_md(doc: dict[str, Any], clip: dict[str, Any], facts: dict[str, Any], txt: str) -> str:
    """中文对照给人审：左边是原片事实，右边是要喂 H3 的英文。"""
    zh = doc.get("zh") or {}
    beats = "\n".join(f"- {b}" for b in (zh.get("beats") or [])) or "-（没写）"
    people = "\n".join(
        f"- ({s.get('id')}) {s.get('zh') or ''}\n  - lock: {s.get('lock')}\n  - voice: {s.get('voice') or ''}"
        for s in (doc.get("speakers") or [])
    ) or "-（本段没有说话人）"
    speech = "\n".join(
        f"- {_fmt(s['a'])}-{_fmt(s['b'])}s 「{s['text']}」（{s['emotion'] or '未标'}）"
        for s in facts["speech"]
    ) or "-（本段没有对白）"
    neighbor = "\n".join(
        f"- {n['id']} `{_fmt(n['t0'])}-{_fmt(n['t1'])}`：" + " / ".join(n["lines"])
        for n in (facts.get("neighbors") or [])
    ) or "-（无）"
    vision = "\n".join(
        f"- {_fmt(_local(a, clip))}-{_fmt(_local(b, clip))}s {see}" for a, b, see in facts["vision"]
    ) or "-（空，按附图）"
    actions = "\n".join(f"- {line}" for line in (facts.get("actions") or [])) or "-（无）"
    cuts = "、".join(f"{_fmt(c)}s" for c in facts["cuts"]) or "无"
    headcount = f"成人 {facts.get('adults')}，儿童 {facts.get('children')}"
    return f"""# {clip["id"]}

源片 `{_fmt(clip["t0"])}-{_fmt(clip["t1"])}` ｜ 成片 `{_fmt(clip["h3_seconds"])}s`（{clip["h3_frames"]} 帧，网格漂移 {clip["drift"]:+.2f}s）
段内硬切：{cuts}
拍表人数：{headcount}

## 事件链

{zh.get("event_chain") or "（没写）"}

## 表演节拍（中文）

{beats}

## 幅度

{zh.get("amplitude") or "（没写）"}

## 人物

{people}

## Scene lock

{doc.get("scene_lock") or "（没写）"}

## 对白原文

{speech}

## 邻条对白（不要跟读）

{neighbor}

## 窗级动作链

{actions}

## 8B 读出来的画面

{vision}

## 待确认

{zh.get("note") or "（无）"}

## 喂给 H3 的英文

```text
{txt.strip()}
```
"""


def assemble_zh(doc: dict[str, Any], clip: dict[str, Any]) -> str:
    """纯中文编辑稿：用户唯一可改的内容。不含英文 H3 正文、[Shot] 标记、时间戳。

    对白原文另列在界面里（只读），这里只给可理解的中文脚本。
    """
    zh = doc.get("zh") or {}
    beats = "\n".join(f"- {b}" for b in (zh.get("beats") or [])) or "（没写）"
    people = "\n".join(
        f"- ({s.get('id')}) {s.get('zh') or ''}" for s in (doc.get("speakers") or [])
    ) or "（本段没有说话人）"
    header = f"{clip['id']}\n源片 {_fmt(clip['t0'])}-{_fmt(clip['t1'])} ｜ 成片 {_fmt(clip['h3_seconds'])}s"
    return "\n\n".join(
        [
            header,
            f"## 事件链\n{zh.get('event_chain') or '（没写）'}",
            f"## 表演节拍\n{beats}",
            f"## 幅度\n{zh.get('amplitude') or '（没写）'}",
            f"## 场景\n{zh.get('scene') or '（没写）'}",
            f"## 环境音\n{zh.get('soundscape') or '（没写）'}",
            f"## 人物\n{people}",
            f"## 待确认\n{zh.get('note') or '（无）'}",
        ]
    )


_ZH_HEADINGS = {
    "事件链": "event_chain",
    "表演节拍": "beats",
    "幅度": "amplitude",
    "场景": "scene",
    "环境音": "soundscape",
    "待确认": "note",
}
_ZH_EMPTY = {"（没写）", "（无）", "（本段没有说话人）"}
_MOCK_SPEEDS = {"0.25x": 0.25, "1x": 1.0, "4x": 4.0}


def _zh_from_editor(text: str) -> dict[str, Any]:
    """从中文编辑稿里抠出可写回 JSON 的字段。人物锁仍走 _preserve_locks，这里不改。"""
    out: dict[str, Any] = {}
    current: str | None = None
    chunks: dict[str, list[str]] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            title = line[3:].split("（", 1)[0].strip()
            current = _ZH_HEADINGS.get(title)
            continue
        if not current:
            continue
        chunks.setdefault(current, []).append(raw.rstrip())
    for key, lines in chunks.items():
        body = "\n".join(lines).strip()
        if not body or body in _ZH_EMPTY:
            continue
        if key == "beats":
            beats = []
            for item in body.splitlines():
                item = item.strip()
                if item.startswith("- "):
                    item = item[2:].strip()
                elif item.startswith("-"):
                    item = item[1:].strip()
                if item and item not in _ZH_EMPTY:
                    beats.append(item)
            if beats:
                out["beats"] = beats
        else:
            out[key] = body
    return out


def _mock_wait(settings: Settings) -> None:
    speed = _MOCK_SPEEDS.get(settings.mock_speed(), 1.0)
    time.sleep(max(0.05, 0.15 / speed))


def _mock_fix_shots(shots: list[dict[str, Any]], seconds: float) -> list[dict[str, Any]]:
    if not shots:
        return [{"index": 1, "at": None, "text": ""}]
    out = [dict(shot) for shot in shots]
    out[0]["index"] = 1
    out[0]["at"] = None
    prev = 0.0
    kept = [out[0]]
    for i, shot in enumerate(out[1:], start=2):
        try:
            at = float(shot.get("at"))
        except (TypeError, ValueError):
            continue
        if at - prev < MIN_SHOT or seconds - at < MIN_SHOT:
            continue
        shot["index"] = i
        shot["at"] = at
        kept.append(shot)
        prev = at
        break
    for i, shot in enumerate(kept, start=1):
        shot["index"] = i
    return kept


def _mock_lock_prefix(doc: dict[str, Any], speakers: list[dict[str, Any]]) -> str:
    scene = str(doc.get("scene_lock") or "").strip() or (
        "Scene lock: a clean studio with controlled soft light, neutral surfaces, and a steady camera."
    )
    if "scene lock" not in scene.lower():
        scene = f"Scene lock: {scene}"
    if speakers:
        first = speakers[0]
        ident = str(first.get("lock") or "").strip() or (
            "Identity lock: an adult with a consistent face, hair, and layered clothing."
        )
        if "identity lock" not in ident.lower():
            ident = f"Identity lock: {ident}"
        voice = str(first.get("voice") or "").strip() or (
            "Voice lock: adult Chinese speech, mid pitch, even rate."
        )
        if "voice lock" not in voice.lower():
            voice = f"Voice lock: {voice}"
        return f"{ident} {voice} {scene}"
    return (
        "Identity lock: no speaking characters, clear geometric subjects. "
        f"{scene}"
    )


def _mock_with_dialogue(body: str, allowed: list[str], speakers: list[dict[str, Any]]) -> str:
    text = str(body or "").strip()
    sid = str((speakers[0].get("id") if speakers else "") or "S1")
    tag = f"({sid})"
    for line in allowed:
        said = str(line or "").strip()
        if not said:
            continue
        if said in text:
            continue
        text = f"{text} {tag} says <d>[Chinese] {said}</d> then holds still.".strip()
    return text


def _mock_rewrite_clip(
    settings: Settings,
    clip: dict[str, Any],
    facts: dict[str, Any],
    *,
    path: str,
    current: dict[str, Any],
    edit: dict[str, Any],
    log: Any,
) -> tuple[dict[str, Any], str]:
    """Mock 模式不调真实模型：在现有 JSON 上套中文改动，保证过机检。"""
    _mock_wait(settings)
    seconds = float(clip["h3_seconds"])
    allowed = [s["text"] for s in facts["speech"]]
    doc = copy.deepcopy(current) if isinstance(current, dict) else {}
    zh = dict(doc.get("zh") or {})
    parsed = _zh_from_editor(str(edit.get("script_zh") or ""))
    zh.update(parsed)
    requirement = str(edit.get("requirement") or "").strip()
    if requirement:
        prior = str(zh.get("note") or "").strip()
        mark = f"Mock 已应用：{requirement[:80]}"
        zh["note"] = f"{prior}；{mark}" if prior and prior not in _ZH_EMPTY else mark
    zh.setdefault("event_chain", "画面出现→构图移动→稳定收束")
    zh.setdefault("beats", ["开场建立构图", "中段构图移动", "结尾稳定收束"])
    zh.setdefault("amplitude", "中等；动作连续；结尾稳定")
    zh.setdefault("scene", "干净影棚，柔光，中性表面")
    zh.setdefault("soundscape", "安静室内底噪，无对白")
    doc["zh"] = zh
    doc["style"] = "live-action photorealistic"
    if not str(doc.get("scene_lock") or "").strip():
        doc["scene_lock"] = (
            "Scene lock: a clean studio with controlled soft light, neutral surfaces, and a steady camera."
        )
    doc["non_diegetic_music"] = "N/A"
    if not str(doc.get("overall_soundscape") or "").strip():
        doc["overall_soundscape"] = (
            "A quiet studio room tone with a soft electronic movement and no spoken dialogue."
        )
    speakers = list(doc.get("speakers") or [])
    if allowed and not speakers:
        speakers = [
            {
                "id": "S1",
                "lock": "Identity lock: an adult with a consistent face, hair, and layered clothing.",
                "voice": "Voice lock: adult Chinese speech, mid pitch, even rate.",
                "zh": "说话人",
            }
        ]
    doc["speakers"] = speakers
    shots = _mock_fix_shots(list(doc.get("shots") or []), seconds)
    body = str(shots[0].get("text") or "")
    low = body.lower()
    prefix = _mock_lock_prefix(doc, speakers)
    if "identity lock" not in low or "scene lock" not in low or (speakers and "voice lock" not in low):
        body = f"{prefix} {body}".strip()
    if "mock rewrite" not in low:
        body = f"{body} Mock rewrite keeps the same blocking while the composition settles.".strip()
    shots[0]["text"] = _mock_with_dialogue(body, allowed, speakers)
    doc["shots"] = shots
    doc["clip_id"] = clip["id"]
    doc["generate_path"] = path
    if isinstance(current, dict):
        _preserve_locks(current, doc)
    txt = assemble_txt(doc, clip, path)
    errors = (
        check_clip(doc, seconds, allowed)
        + check_header(clip["id"], txt, seconds)
        + check_mode(clip["id"], txt, wants_keyframe=bool(PATH_KEYFRAMES.get(path)))
    )
    if errors:
        shots = [{"index": 1, "at": None, "text": ""}]
        body = (
            f"{prefix} Mock rewrite keeps the same blocking while the composition settles."
        )
        shots[0]["text"] = _mock_with_dialogue(body, allowed, speakers)
        doc["shots"] = shots
        txt = assemble_txt(doc, clip, path)
        errors = (
            check_clip(doc, seconds, allowed)
            + check_header(clip["id"], txt, seconds)
            + check_mode(clip["id"], txt, wants_keyframe=bool(PATH_KEYFRAMES.get(path)))
        )
    if errors:
        raise PassBError(f"{clip['id']} mock 改写未过检：{errors[0]}")
    doc["writer_rev"] = WRITER_REV
    doc["source_t0"] = float(clip["t0"])
    doc["source_t1"] = float(clip["t1"])
    log(f"  {clip['id']} mock 改写过检（未调用真实模型）")
    return doc, txt


def _preserve_locks(current: dict[str, Any], new: dict[str, Any]) -> None:
    """改写后强制复用旧的身份锁，避免同一人物换脸、跨段外观不一致。"""
    old = {str(s.get("id")): s for s in (current.get("speakers") or [])}
    for speaker in new.get("speakers") or []:
        sid = str(speaker.get("id") or "")
        prior = old.get(sid)
        if not prior:
            continue
        if str(prior.get("lock") or "").strip():
            speaker["lock"] = prior["lock"]
        if str(prior.get("voice") or "").strip():
            speaker["voice"] = prior["voice"]


def build_rewrite_prompt(
    clip: dict[str, Any],
    facts: dict[str, Any],
    *,
    path: str,
    current: dict[str, Any],
    edit: dict[str, Any],
    errors: list[str] | None = None,
) -> str:
    """在既有结构化脚本上，按用户的中文诉求重写并重新过检。"""
    seconds = float(clip["h3_seconds"])
    speech = "\n".join(
        f"  {_fmt(s['a'])}-{_fmt(s['b'])}  「{s['text']}」（语气 {s['emotion'] or '未标'}）"
        for s in facts["speech"]
    ) or "  （本段没有对白）"
    vision = "\n".join(
        f"  {_fmt(_local(a, clip))}-{_fmt(_local(b, clip))}  {see}" for a, b, see in facts["vision"]
    ) or "  （拍表格是空的，按附图写）"
    cuts = "、".join(_fmt(c) + "s" for c in facts["cuts"]) or "（未检）"
    actions = "\n".join(f"  {line}" for line in (facts.get("actions") or [])) or "  （无）"
    mouths = "\n".join(f"  {m}" for m in (facts.get("mouths") or [])) or "  （未标）"
    frames = "\n".join(
        f"  {_fmt(_local(t, clip))}s（源片 {_fmt(t)}s）" for t, _p in (facts["frames"] or [])
    ) or "  （没有可用帧）"
    neighbor_block = "  （没有邻条对白）"
    if facts.get("neighbors"):
        rows = []
        for item in facts["neighbors"]:
            rows.append(f"  {item['id']} 源片 {_fmt(item['t0'])}-{_fmt(item['t1'])}s：" + " / ".join(item["lines"]))
        neighbor_block = "\n".join(rows)
    locked = "\n".join(
        f"  {s.get('id')}：{s.get('lock')}" for s in (current.get("speakers") or [])
    ) or "  （无）"

    if PATH_KEYFRAMES.get(path):
        mission = "你在改写一条 MiniMax H3 视频生成提示词。这是复刻原片，不是二创。"
        frames_head = "附图（外观、站位、道具、光线以图为准）"
        lock_rule = "说话人用 (S1)(S2)，已锁定的外观必须逐字复用，不许改写"
    else:
        mission = "你在改写一条 MiniMax H3 视频生成提示词。这是复刻原片的情节，不是摘要。"
        frames_head = "附图（情节、场次、站位、道具、光线以图为准）"
        lock_rule = "说话人用 (S1)(S2)，本条自己写完整 Identity lock 和 Voice lock"

    mode = str(edit.get("mode") or "ai")
    if mode == "manual":
        instruction = (
            "用户改后的中文脚本如下（这是唯一权威，英文正文按它重写；"
            "事件链/表演节拍/幅度/场景/环境音/人物中文要照搬进 zh，再据此重写英文 shots）：\n\n"
            f"```\n{edit.get('script_zh') or ''}\n```"
        )
    else:
        instruction = f"用户要求：\n\n{edit.get('requirement') or '（未写）'}"

    retry = ""
    if errors:
        retry = "\n## 上一版没过机检，只改这些\n\n" + "\n".join(f"- {e}" for e in errors) + "\n"

    return f"""{mission}

## 必须保留、不能改

对白原文（进 <d> 必须逐字照抄，一个字都不能改、不能翻译、不能删）：
{speech}

邻条对白（禁止写进本条任何 <d>）：
{neighbor_block}

已锁定说话人外观（必须逐字复用，不许改写）：
{locked}

## 画面事实（以图为准，拍表只是索引）

{frames_head}：
{frames}

8B 读出来的画面：
{vision}

窗级动作链：
{actions}

嘴型：
{mouths}

自动检测到的硬切（本条内部时间，参考，会漏）：{cuts}

## 本条

片段 {clip["id"]}｜源片 {_fmt(clip["t0"])}-{_fmt(clip["t1"])}s｜成片时长 {_fmt(seconds)}s（{clip["h3_frames"]} 帧）

## 改写诉求

{instruction}

## 规矩

- 全部英文，只有 <d> 里面能出现汉字；<d> 写成 `<d>[Chinese] 原句</d>`
- 对白一字不改地落进某个镜头；邻条对白一个字都不写
- 禁止 subtitle / caption / burned-in / on-screen text / Chinese text overlay
- {lock_rule}
- [Shot 1] 正文开头必须出现 Identity lock、Voice lock、Scene lock 三句，后面才是动作
- [Shot 1] 开场必须对上附图第 1 张；末帧只停在附图最后一张的人和景
- 英文动作用 From a.aa to b.bb 写出至少三截节拍，铺满 0 到 {_fmt(seconds)}s
- zh.beats 用本条内部时间，同样至少三截
- shots[0].at 必须是 null；后面的 at 严格递增且小于 {_fmt(seconds)}
- 每一镜至少 {MIN_SHOT:.1f}s（含最后一镜）
- style 只写 live-action photorealistic；non_diegetic_music 必须是 N/A
- overall_soundscape 只写环境音、动作音、非语言人声，不要重复对白、不要配乐
{retry}
## 只输出这个 JSON，不要围栏不要解释

{SCHEMA}
"""


def rewrite_clip(
    settings: Settings,
    clip: dict[str, Any],
    facts: dict[str, Any],
    *,
    path: str,
    current: dict[str, Any],
    edit: dict[str, Any],
    log: Any,
) -> tuple[dict[str, Any], str]:
    """按中文诉求改写一条，走同一套机检；失败抛 PassBError，不改盘。"""
    if settings.mode() == "mock":
        return _mock_rewrite_clip(
            settings, clip, facts, path=path, current=current, edit=edit, log=log
        )
    seconds = float(clip["h3_seconds"])
    allowed = [s["text"] for s in facts["speech"]]
    errors: list[str] = []
    last = "未知错误"
    for attempt in range(MAX_TRIES):
        stats: dict[str, Any] = {}
        prompt = build_rewrite_prompt(clip, facts, path=path, current=current, edit=edit, errors=errors or None)
        try:
            images = [p for _t, p in _spread(list(facts["frames"] or []), 4)]
            raw = generate_text(settings, prompt, images=images or None, stats=stats)
            doc = parse_json_payload(raw, require="shots")
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            last = str(exc)
            log(f"  {clip['id']} 改写第 {attempt + 1} 次：{last}")
            continue
        if not isinstance(doc, dict):
            last = "返回的不是 JSON 对象"
            continue
        doc["clip_id"] = clip["id"]
        doc["generate_path"] = path
        _preserve_locks(current, doc)
        txt = assemble_txt(doc, clip, path)
        errors = (
            check_clip(doc, seconds, allowed)
            + check_header(clip["id"], txt, seconds)
            + check_mode(clip["id"], txt, wants_keyframe=bool(PATH_KEYFRAMES.get(path)))
        )
        if not errors:
            doc["writer_rev"] = WRITER_REV
            doc["source_t0"] = float(clip["t0"])
            doc["source_t1"] = float(clip["t1"])
            log(f"  {clip['id']} 改写过检（{_fmt_stats(stats)}）")
            return doc, txt
        last = f"{len(errors)} 项机检未过"
        log(f"  {clip['id']} 改写第 {attempt + 1} 次 {last}：{errors[0]}")
    raise PassBError(f"{clip['id']} 改写 {MAX_TRIES} 次仍未过检：{last}")


def _one_clip(
    settings: Settings,
    clip: dict[str, Any],
    facts: dict[str, Any],
    *,
    path: str,
    locks: dict[str, str],
    log: Any,
) -> tuple[dict[str, Any], str]:
    seconds = float(clip["h3_seconds"])
    allowed = [s["text"] for s in facts["speech"]]
    errors: list[str] = []
    last = "未知错误"
    for attempt in range(MAX_TRIES):
        stats: dict[str, Any] = {}
        prompt = build_prompt(clip, facts, path=path, locks=locks, errors=errors or None)
        try:
            images = [p for _t, p in _spread(list(facts["frames"] or []), 4)]
            if attempt >= MAX_TRIES - 1:
                images = []
            log(f"  {clip['id']} 第 {attempt + 1} 次写稿（附图 {len(images)} 张）")
            raw = generate_text(settings, prompt, images=images or None, stats=stats)
            doc = parse_json_payload(raw, require="shots")
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            last = str(exc)
            log(f"  {clip['id']} 第 {attempt + 1} 次：{last}")
            continue
        if not isinstance(doc, dict):
            last = "返回的不是 JSON 对象"
            continue
        doc["clip_id"] = clip["id"]
        doc["generate_path"] = path
        txt = assemble_txt(doc, clip, path)
        errors = (
            check_clip(doc, seconds, allowed)
            + check_header(clip["id"], txt, seconds)
            + check_mode(clip["id"], txt, wants_keyframe=bool(PATH_KEYFRAMES.get(path)))
        )
        if not errors:
            doc["writer_rev"] = WRITER_REV
            doc["source_t0"] = float(clip["t0"])
            doc["source_t1"] = float(clip["t1"])
            log(f"  {clip['id']} 过检（{_fmt_stats(stats)}）")
            return doc, txt
        last = f"{len(errors)} 项机检未过"
        log(f"  {clip['id']} 第 {attempt + 1} 次 {last}：{errors[0]}")
    raise PassBError(f"{clip['id']} 写了 {MAX_TRIES} 次仍未过检：{last}")


def _fmt_stats(stats: dict[str, Any]) -> str:
    bits = []
    if stats.get("model"):
        bits.append(str(stats["model"]))
    if stats.get("tok_in") or stats.get("tok_out"):
        bits.append(f"in {stats.get('tok_in', 0)} out {stats.get('tok_out', 0)}")
    if stats.get("ms"):
        bits.append(f"{float(stats['ms']) / 1000:.1f}s")
    for item in stats.get("trace") or []:
        bits.append(f"退回[{item}]")
    return "，".join(bits) or "无统计"


def _apply_canonical_locks(doc: dict[str, Any], locks: dict[str, str]) -> bool:
    """已见过的说话人，外观锁钉成第一次写下的那句。

    模型经常只差一个句号，按冲突整段重写太贵；H3 要的是同一张脸同一句锁。
    """
    changed = False
    for speaker in doc.get("speakers") or []:
        sid = str(speaker.get("id") or "").strip()
        lock = str(speaker.get("lock") or "").strip()
        if not sid or not lock:
            continue
        if sid in locks:
            if str(speaker.get("lock") or "") != locks[sid]:
                speaker["lock"] = locks[sid]
                changed = True
        else:
            locks[sid] = lock
    return changed


def _cached(
    path_json: Path, clip: dict[str, Any], facts: dict[str, Any], path: str
) -> tuple[dict[str, Any], str] | None:
    """上一轮写好又过检的段不重烧。改了机检口径的话，过不了就自动重写。"""
    if not path_json.is_file():
        return None
    try:
        doc = json.loads(path_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    stored = str(doc.get("generate_path") or "")
    if stored and stored != path:
        return None
    if int(doc.get("writer_rev") or 0) != WRITER_REV:
        return None
    if abs(float(doc.get("source_t0") or -1) - float(clip["t0"])) > 0.02:
        return None
    if abs(float(doc.get("source_t1") or -1) - float(clip["t1"])) > 0.02:
        return None
    seconds = float(clip["h3_seconds"])
    txt = assemble_txt(doc, clip, path)
    allowed = [s["text"] for s in facts["speech"]]
    if (
        check_clip(doc, seconds, allowed)
        or check_header(clip["id"], txt, seconds)
        or check_mode(clip["id"], txt, wants_keyframe=bool(PATH_KEYFRAMES.get(path)))
    ):
        return None
    return doc, txt


def write_prompts(
    settings: Settings,
    clips: list[dict[str, Any]],
    beats: dict[str, Any],
    dialogue: dict[str, Any],
    cuts: list[float],
    *,
    directory: Path,
    path: str,
    log: Any,
) -> list[dict[str, Any]]:
    """按顺序写每段。I2VA 把说话人外观锁向后传递；T2VA 不跨段锁脸。"""
    path = normalize_generate_path(path)
    out_dir = directory / "prompts"
    out_dir.mkdir(parents=True, exist_ok=True)
    locks: dict[str, str] = {}
    lock_across = bool(PATH_LOCK_ACROSS.get(path, True))
    if not lock_across:
        log("T2VA：不跨段传递外观锁；人物不必像原片，但锁句和动作链要写细")
    prepared: list[tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], str] | None]] = []
    for clip in clips:
        facts = clip_facts(clip, beats, dialogue, cuts, root=directory, clips=clips)
        cached = _cached(out_dir / f"{clip['id']}.json", clip, facts, path)
        prepared.append((clip, facts, cached))

    items: list[dict[str, Any]] = []
    for clip, facts, cached in prepared:
        if not lock_across or clip.get("cast_reset"):
            locks.clear()
            if lock_across and clip.get("cast_reset"):
                log(f"  {clip['id']} 人物组重置，清空已锁外观")
        if cached is not None:
            doc, txt = cached
            snapped = _apply_canonical_locks(doc, locks) if lock_across else False
            doc["generate_path"] = path
            if snapped:
                txt = assemble_txt(doc, clip, path)
                (out_dir / f"{clip['id']}.json").write_text(
                    json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                log(f"  {clip['id']} 复用已过检的结果（已钉外观锁）")
            else:
                log(f"  {clip['id']} 复用已过检的结果")
        else:
            use_locks = locks if lock_across else {}
            doc, txt = _one_clip(settings, clip, facts, path=path, locks=use_locks, log=log)
            if lock_across:
                _apply_canonical_locks(doc, locks)
            doc["generate_path"] = path
            (out_dir / f"{clip['id']}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        stem = str(clip["id"])
        (out_dir / f"{stem}.txt").write_text(txt, encoding="utf-8")
        (out_dir / f"{stem}.md").write_text(
            assemble_md(doc, clip, facts, txt), encoding="utf-8"
        )
        items.append(
            {
                "clip_id": clip["id"],
                "generate_path": path,
                "h3_seconds": clip["h3_seconds"],
                "prompt": f"prompts/{stem}.txt",
                "review": f"prompts/{stem}.md",
                "speakers": doc.get("speakers") or [],
                "shots": [
                    {"index": s.get("index"), "at": s.get("at")} for s in (doc.get("shots") or [])
                ],
                "cast_reset": bool(clip.get("cast_reset")),
            }
        )
    return items
