"""按 OCR 分区生成 ASS：底栏对白、居中戏剧核。水印/角标不烧。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vrs.deliver import concat_spans, map_span
from vrs.dialogue import is_dialogue_caption

_SKIP_TITLE = re.compile(
    r"AI生成|仅供娱乐|虚拟演绎|喵喵|原创|无不良|打开抖音|来抖音|关注我|本片由"
)
_DRAMA = re.compile(r"[（(].{4,30}[）)]")


def _ass_time(seconds: float) -> str:
    t = max(0.0, float(seconds))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    cs = int(round((s - int(s)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{int(s) % 60:02d}.{cs:02d}"


def _escape(text: str) -> str:
    out = (text or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
    return out.replace("\n", r"\N")


def _is_drama_title(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _SKIP_TITLE.search(raw):
        return False
    if _DRAMA.search(raw):
        return True
    return "最" in raw and 6 <= len(raw) <= 28


def _events_from_dialogue(
    dialogue: dict[str, Any],
    spans: list[dict[str, Any]],
    *,
    default_region: str,
) -> list[tuple[str, float, float, str]]:
    rows: list[tuple[str, float, float, str]] = []
    for item in dialogue.get("speech") or []:
        text = str(item.get("text") or "").strip()
        if not is_dialogue_caption(text):
            continue
        mapped = map_span(float(item.get("t0") or 0), float(item.get("t1") or 0), spans)
        if mapped:
            rows.append((default_region if default_region in {"bottom", "title"} else "bottom", *mapped, text))
    for item in dialogue.get("on_screen") or []:
        if item.get("watermark"):
            continue
        text = str(item.get("text") or "").strip()
        region = str(item.get("region") or "")
        if region not in {"title", "top", "upper"}:
            continue
        if not _is_drama_title(text):
            continue
        mapped = map_span(float(item.get("t0") or 0), float(item.get("t1") or 0), spans)
        if mapped:
            rows.append(("title", *mapped, text))
    return rows


def write_ass(
    dest: Path,
    dialogue: dict[str, Any],
    clips: list[dict[str, Any]],
    *,
    default_region: str = "bottom",
    play_res: tuple[int, int] = (1920, 1080),
) -> int:
    spans = concat_spans(clips)
    events = _events_from_dialogue(dialogue, spans, default_region=default_region)
    width, height = play_res
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bottom,Microsoft YaHei,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,0,2,40,40,48,1
Style: Title,Microsoft YaHei,64,&H0000FFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,0,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for style_key, t0, t1, text in events:
        style = "Title" if style_key == "title" else "Bottom"
        lines.append(
            f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},{style},,0,0,0,,{_escape(text)}\n"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(lines), encoding="utf-8-sig")
    return len(events)
