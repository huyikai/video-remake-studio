"""H3 提示词的机检口径。

全是纯函数，precheck 阶段和 Pass B 的落盘前自检共用同一套判据。
写得好不好没法机检，但「编了对白」「节拍超出时长」「同一个人换了脸」可以。
"""

from __future__ import annotations

import re
from typing import Any

HAN = re.compile(r"[\u4e00-\u9fff]")
D_BLOCK = re.compile(r"<d>(.*?)</d>", re.S)
LANG_TAG = re.compile(r"^\s*\[[A-Za-z]+\]\s*")
HEADER_SEC = re.compile(r"aligns with the (\d+\.\d{2})-second mark")

# 官方规格 4.4：说话人 ID 是 (S1)、(S2)，合说写 (S1,S2)
SPEAKER = re.compile(r"\(S\d+(?:,S\d+)*\)")
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:—…「」『』“”\"'()（）]+")

# H3 会把这些词理解成「请把字幕烧进画面」。不要写进提示词，也不要写进 Avoid。
BANNED = (
    "subtitle",
    "caption",
    "burned-in",
    "chinese text overlay",
    "text overlay",
    "on-screen text",
    "on-screen caption",
)
I2VA_MARK = "<Picture 1>"
CORE_FIELDS = (
    "integrated_multimodal_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def d_payloads(text: str) -> list[str]:
    """取出每个 <d> 里的实际台词，语言标签不算台词。"""
    return [LANG_TAG.sub("", inner).strip() for inner in D_BLOCK.findall(str(text or ""))]


def check_dialogue(clip_id: str, text: str, allowed: list[str], *, lang: str = "Chinese") -> list[str]:
    """<d> 里的每句必须逐字来自 dialogue.json，标签必须是语言而不是语气。"""
    pool = {_norm(a) for a in allowed}
    out: list[str] = []
    for inner in D_BLOCK.findall(str(text or "")):
        tag = LANG_TAG.match(inner)
        if not tag:
            out.append(f"{clip_id}: <d> 缺 [{lang}] 语言标签")
        elif tag.group(0).strip() != f"[{lang}]":
            out.append(f"{clip_id}: <d> 的标签写成了 {tag.group(0).strip()}，只能是 [{lang}]")
        said = LANG_TAG.sub("", inner).strip()
        if _norm(said) not in pool:
            out.append(f"{clip_id}: <d> 里的「{said[:24]}」不在本段对白里，是编的")
    return out


def check_han(clip_id: str, text: str) -> list[str]:
    """汉字只允许出现在 <d> 里。英文三字段里混中文，H3 会当成要画的字。"""
    stripped = D_BLOCK.sub("", str(text or ""))
    found = HAN.findall(stripped)
    if not found:
        return []
    return [f"{clip_id}: <d> 之外出现了 {len(found)} 个汉字（{''.join(found[:8])}）"]


# 24fps 下不到这个长度的「镜头」只有十几帧，H3 演不出任何东西
MIN_SHOT = 0.6


def check_shots(
    clip_id: str, shots: list[dict[str, Any]], seconds: float, *, min_shot: float = MIN_SHOT
) -> list[str]:
    """官方规格 4.2：首镜不带时间戳，后面每一镜的切点严格递增且落在时长内。

    再加一条规格没写但必须的：每一镜要有实际长度。切在 0.15s 的第二镜等于没有首镜。
    """
    out: list[str] = []
    if not shots:
        return [f"{clip_id}: 一个镜头都没有"]
    if shots[0].get("at") is not None:
        out.append(f"{clip_id}: [Shot 1] 不能带时间戳")
    prev = 0.0
    for shot in shots[1:]:
        at = shot.get("at")
        if at is None:
            out.append(f"{clip_id}: [Shot {shot.get('index')}] 缺切点时间")
            continue
        at = float(at)
        if at >= seconds - 1e-6:
            out.append(f"{clip_id}: [Shot {shot.get('index')}] 切点 {at:.2f}s 超出时长 {seconds:.2f}s")
        elif at - prev < min_shot:
            out.append(
                f"{clip_id}: [Shot {shot.get('index')}] 切在 {at:.2f}s，上一镜只有 "
                f"{at - prev:.2f}s，不足 {min_shot:.2f}s"
            )
        prev = at
    if shots[1:] and seconds - prev < min_shot:
        out.append(f"{clip_id}: 最后一镜只有 {seconds - prev:.2f}s，不足 {min_shot:.2f}s")
    return out


def check_header(clip_id: str, prompt: str, seconds: float) -> list[str]:
    """首行对齐指令里的 S.SS 必须等于本条实际时长，差了 H3 就按错的时长铺。"""
    found = HEADER_SEC.findall(str(prompt or ""))
    if not found:
        return []
    out = []
    for value in found:
        if abs(float(value) - seconds) > 0.005:
            out.append(f"{clip_id}: 首行写 {value}s，本条时长 {seconds:.2f}s，对不上")
    return out


def check_speakers(clip_id: str, text: str, shots_text: str) -> list[str]:
    """出现在正文里的说话人 ID 必须都在人物表里定义过，否则 H3 不知道是谁在说。"""
    declared = {s for s in SPEAKER.findall(text)}
    used: set[str] = set()
    for item in SPEAKER.findall(shots_text):
        used.update(f"(S{n})" for n in re.findall(r"S(\d+)", item))
    missing = sorted(used - declared)
    if missing:
        return [f"{clip_id}: 正文用了没定义的说话人 {'、'.join(missing)}"]
    return []


ABSTRACT = re.compile(r"\b(emotional|beautiful|expressive|cinematic)\b", re.I)


def check_density(clip_id: str, shots_text: str, *, speakers: list[dict[str, Any]]) -> list[str]:
    """H3 只演写出来的东西。缺锁句或只有情绪词，生成出来就是空的。"""
    out: list[str] = []
    body = str(shots_text or "")
    low = body.lower()
    if "identity lock" not in low:
        out.append(f"{clip_id}: 正文缺 Identity lock，外观会被写糊")
    if "scene lock" not in low:
        out.append(f"{clip_id}: 正文缺 Scene lock，场景会被写糊")
    if speakers and "voice lock" not in low:
        out.append(f"{clip_id}: 正文缺 Voice lock")
    hits = ABSTRACT.findall(body)
    if hits:
        out.append(f"{clip_id}: 不要写 {', '.join(sorted({h.lower() for h in hits}))}，改成看得见的动作和光线")
    return out


def check_locks(docs: list[dict[str, Any]]) -> list[str]:
    """同一个说话人 ID 的外观锁必须跨段逐字一样，差一个词就是换了张脸。

    人物组重置后 S 号从本段重新起，和重置前的同号不是同一个人。
    """
    seen: dict[str, tuple[str, str]] = {}
    out: list[str] = []
    for doc in docs:
        clip_id = str(doc.get("clip_id") or "?")
        if doc.get("cast_reset"):
            seen.clear()
        for speaker in doc.get("speakers") or []:
            sid = str(speaker.get("id") or "")
            lock = str(speaker.get("lock") or "").strip()
            if not sid or not lock:
                continue
            prior = seen.get(sid)
            if prior is None:
                seen[sid] = (clip_id, lock)
            elif _norm(prior[1]) != _norm(lock):
                out.append(
                    f"{clip_id}: {sid} 的外观锁和 {prior[0]} 不一致\n"
                    f"    {prior[0]}: {prior[1][:70]}\n"
                    f"    {clip_id}: {lock[:70]}"
                )
    return out


def _bare(text: str) -> str:
    return PUNCT.sub("", str(text or ""))


def speech_owner_id(t0: float, t1: float, clips: list[dict[str, Any]]) -> str | None:
    """一句台词只归重叠最多的那一段。二次拆分切在句中时，不要两边都写、后面跟读。"""
    best_id: str | None = None
    best_ov = 0.0
    for clip in clips:
        overlap = max(0.0, min(t1, float(clip["t1"])) - max(t0, float(clip["t0"])))
        if overlap > best_ov:
            best_ov = overlap
            best_id = str(clip["id"])
    if best_ov <= 0.05:
        return None
    return best_id


def iter_clip_speech(
    clip: dict[str, Any],
    dialogue: dict[str, Any],
    clips: list[dict[str, Any]] | None = None,
):
    peers = list(clips) if clips else [clip]
    cid = str(clip["id"])
    for item in dialogue.get("speech") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if speech_owner_id(float(item["t0"]), float(item["t1"]), peers) == cid:
            yield item


def speech_for_clip(
    clip: dict[str, Any],
    dialogue: dict[str, Any],
    clips: list[dict[str, Any]] | None = None,
) -> list[str]:
    return [str(s.get("text") or "").strip() for s in iter_clip_speech(clip, dialogue, clips)]


def check_coverage(clip_id: str, text: str, required: list[str]) -> list[str]:
    """dialogue.json 里本段的每一句都必须进某个 <d>。拦漏对白。"""
    written = [_bare(x) for x in d_payloads(text)]
    out: list[str] = []
    for line in required:
        key = _bare(line)
        if not key:
            continue
        if not any(key in w or w in key for w in written if w):
            out.append(f"{clip_id}: 对白「{line[:24]}」没有进任何 <d>，会漏一句")
    return out


def check_repeat(clip_id: str, text: str) -> list[str]:
    """同一句在本段出现两次 = 跟读。硬切跨场也只许落一次 <d>。"""
    counts: dict[str, tuple[str, int]] = {}
    for line in d_payloads(text):
        key = _norm(line)
        if not key:
            continue
        shown, n = counts.get(key, (line, 0))
        counts[key] = (shown, n + 1)
    return [
        f"{clip_id}: 「{shown[:24]}」在本段写了 {n} 次，会跟读"
        for shown, n in counts.values()
        if n > 1
    ]


def check_dupes(items: list[tuple[str, str]]) -> list[str]:
    """同一句台词出现在两条 = 跟读。同一条里的重复由 check_repeat 拦。"""
    where: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for clip_id, text in items:
        for line in d_payloads(text):
            key = _norm(line)
            if not key:
                continue
            where.setdefault(key, []).append(clip_id)
            shown.setdefault(key, line)
    return [
        f"「{shown[key][:24]}」同时出现在 {' 和 '.join(dict.fromkeys(hits))}，后一条会跟读"
        for key, hits in where.items()
        if len(set(hits)) > 1
    ]


def check_banned(clip_id: str, prompt: str) -> list[str]:
    low = str(prompt or "").lower()
    return [f"{clip_id}: 英文里出现 `{word}`，会引出烧死的字幕" for word in BANNED if word in low]


def check_fields(clip_id: str, prompt: str) -> list[str]:
    return [f"{clip_id}: 缺 {field.rstrip(':')}" for field in CORE_FIELDS if field not in str(prompt or "")]


def check_mode(clip_id: str, prompt: str, *, wants_keyframe: bool) -> list[str]:
    has = I2VA_MARK in str(prompt or "")
    if wants_keyframe and not has:
        return [f"{clip_id}: I2VA 缺 {I2VA_MARK} 首帧对齐指令"]
    if not wants_keyframe and has:
        return [f"{clip_id}: T2VA 不该出现 {I2VA_MARK}"]
    return []


def check_clip(doc: dict[str, Any], seconds: float, allowed: list[str]) -> list[str]:
    """单条落盘前的全部自检。返回空列表才算过。"""
    clip_id = str(doc.get("clip_id") or "?")
    shots = list(doc.get("shots") or [])
    body = " ".join(str(s.get("text") or "") for s in shots)
    people = " ".join(
        f"({s.get('id')}) {s.get('lock')}" for s in (doc.get("speakers") or [])
    )
    tail = " ".join(
        str(doc.get(k) or "") for k in ("overall_soundscape", "non_diegetic_music")
    )
    out = check_shots(clip_id, shots, seconds)
    out += check_dialogue(clip_id, body, allowed)
    out += check_repeat(clip_id, body)
    out += check_han(clip_id, body + " " + people + " " + tail)
    out += check_speakers(clip_id, people, body)
    out += check_banned(clip_id, body + " " + people + " " + tail)
    out += check_density(clip_id, body, speakers=list(doc.get("speakers") or []))
    if not str(doc.get("style") or "").strip():
        out.append(f"{clip_id}: 缺 style，[Shot 1] 开头没有整体风格")
    for key in ("overall_soundscape", "non_diegetic_music"):
        if not str(doc.get(key) or "").strip():
            out.append(f"{clip_id}: 缺 {key}")
    music = str(doc.get("non_diegetic_music") or "").strip()
    if music and music.upper() != "N/A":
        out.append(f"{clip_id}: non_diegetic_music 必须是 N/A，不要写 BGM")
    return out
