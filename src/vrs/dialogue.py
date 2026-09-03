from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from vrs.asr import _split_long_segments, looks_like_hallucination

_SPACE = re.compile(r"\s+")
_OVERLAY = re.compile(
    r"(会经我|曾經我|曾经我|后来我|社会生活|实践构成|社会历史|害怕人群|害怕火焰|害怕被欺负)"
)
_NOISE = re.compile(r"^[\W_Cｃ＋+\-·.•…\.．。、，,（）()【】\[\]★#]+$")


def _norm(text: str) -> str:
    return _SPACE.sub("", (text or "").strip().lower())


def _similar(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _group_ocr(items: list[dict[str, Any]], fps: float) -> list[dict[str, Any]]:
    usable = [i for i in items if not i.get("watermark")]
    if not usable:
        return []
    usable.sort(key=lambda item: (float(item.get("t") or 0), str(item.get("region") or "")))
    gap = 1.5 / max(fps, 0.1)
    frame = 1.0 / max(fps, 0.1)
    currents: dict[str, dict[str, Any]] = {}
    groups: list[dict[str, Any]] = []
    for item in usable:
        region = str(item.get("region") or "other")
        current = currents.get(region)
        t = float(item.get("t") or 0)
        text = str(item.get("text") or "")
        if current and _similar(text, current["text"]) >= 0.72 and t - float(current["t1"]) <= gap + 0.35:
            current["t1"] = t + frame
            if len(text) > len(str(current["text"])):
                current["text"] = text
            continue
        if current:
            groups.append(current)
        currents[region] = {
            "t0": t,
            "t1": t + frame,
            "text": text,
            "region": region,
        }
    groups.extend(currents.values())
    groups.sort(key=lambda item: (float(item["t0"]), float(item["t1"])))
    return groups


def _cjk_len(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def is_dialogue_caption(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 2 or _NOISE.fullmatch(raw):
        return False
    if _OVERLAY.search(raw):
        return False
    if _cjk_len(raw) < 2:
        return False
    return True


def _persistent_labels(items: list[dict[str, Any]]) -> set[str]:
    spans: dict[str, list[float]] = {}
    for item in items:
        if item.get("region") != "bottom" or item.get("watermark"):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        t = float(item.get("t") or 0)
        if text not in spans:
            spans[text] = [t, t]
        else:
            spans[text][1] = t
    labels: set[str] = set()
    for text, (t0, t1) in spans.items():
        if _cjk_len(text) <= 3 and (t1 - t0) >= 3.0:
            labels.add(_norm(text))
    return labels


def _caption_groups(ocr: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not ocr:
        return []
    items = ocr.get("items") or []
    labels = _persistent_labels(items)
    usable = [
        item
        for item in items
        if item.get("region") == "bottom"
        and not item.get("watermark")
        and is_dialogue_caption(str(item.get("text") or ""))
        and _norm(str(item.get("text") or "")) not in labels
    ]
    return _group_ocr(usable, float(ocr.get("fps") or 2.0))


def ocr_bottom_lines(ocr: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    for item in _caption_groups(ocr):
        text = str(item.get("text") or "").strip()
        if lines and _similar(lines[-1], text) >= 0.85:
            if len(text) > len(lines[-1]):
                lines[-1] = text
            continue
        lines.append(text)
    return lines


def _asr_as_segments(full: dict[str, Any]) -> list[dict[str, Any]]:
    words = list(full.get("words") or [])
    text = str(full.get("text") or "").strip()
    if words:
        blob = [
            {
                "id": 1,
                "start": float(words[0]["start"]),
                "end": float(words[-1]["end"]),
                "text": text,
                "words": words,
            }
        ]
        return [
            item
            for item in _split_long_segments(blob)
            if not looks_like_hallucination(str(item.get("text") or ""))
        ]
    if text:
        return [
            {
                "id": 1,
                "start": 0.0,
                "end": float(full.get("duration") or 0),
                "text": text,
                "words": [],
            }
        ]
    return []


def _full_from_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    if transcript.get("words") is not None and transcript.get("text"):
        return transcript
    words: list[dict[str, Any]] = []
    texts: list[str] = []
    for seg in transcript.get("segments") or []:
        words.extend(list(seg.get("words") or []))
        texts.append(str(seg.get("text_whisper") or seg.get("text_asr") or seg.get("text") or ""))
    out = dict(transcript)
    out["text"] = str(transcript.get("text") or "").strip() or "".join(texts)
    out["words"] = words
    return out


def to_full_transcript(raw: dict[str, Any]) -> dict[str, Any]:
    full = _full_from_transcript(raw)
    out = {
        key: full.get(key)
        for key in (
            "engine",
            "model",
            "aligner",
            "source",
            "aligner_source",
            "device",
            "language",
            "duration",
            "initial_prompt",
        )
        if key in full
    }
    out["text"] = str(full.get("text") or "")
    out["words"] = list(full.get("words") or [])
    return out


def _whisper_span(segments: list[dict[str, Any]], t0: float, t1: float) -> tuple[str, list[dict[str, Any]]]:
    words: list[dict[str, Any]] = []
    for seg in segments:
        for word in seg.get("words") or []:
            ws = float(word.get("start") or 0)
            we = float(word.get("end") or 0)
            overlap = min(t1, we) - max(t0, ws)
            if overlap <= 0:
                continue
            if overlap / max(0.02, we - ws) < 0.55:
                continue
            words.append(word)
    if not words:
        return "", []
    return _join_caption_words(words), words


def _join_caption_words(words: list[dict[str, Any]]) -> str:
    return _SPACE.sub("", "".join(str(w.get("word") or "") for w in words)).strip()


def finalize_transcript(full: dict[str, Any], ocr: dict[str, Any] | None) -> dict[str, Any]:
    """有烧字：正文信 OCR、时间戳信 Aligner；多出的口播另开 asr-extra。无字幕：信整段 ASR。"""
    full = _full_from_transcript(full)
    captions = _caption_groups(ocr)
    asr_segs = _asr_as_segments(full)
    word_bucket = [{"words": list(full.get("words") or [])}]
    out = {
        key: value
        for key, value in full.items()
        if key not in {"segments"}
    }

    if not captions:
        segs = []
        for item in asr_segs:
            segs.append(
                {
                    **item,
                    "source": "asr",
                    "text_asr": str(item.get("text") or ""),
                }
            )
        for index, item in enumerate(segs, start=1):
            item["id"] = index
        out["segments"] = segs
        out["ocr_aligned"] = False
        return out

    new_segs: list[dict[str, Any]] = []
    for cap in captions:
        t0 = float(cap["t0"])
        t1 = float(cap["t1"])
        if t1 - t0 < 0.2:
            t1 = t0 + 0.45
        ocr_text = str(cap.get("text") or "").strip()
        asr_text, span_words = _whisper_span(word_bucket, t0, t1)
        if span_words:
            t0 = float(span_words[0]["start"])
            t1 = float(span_words[-1]["end"])
        new_segs.append(
            {
                "id": 0,
                "start": round(t0, 3),
                "end": round(t1, 3),
                "text": ocr_text,
                "text_asr": asr_text,
                "text_whisper": asr_text,
                "words": span_words,
                "source": "ocr",
                "corrected_by": "ocr-bottom",
                "ocr_score": round(_similar(asr_text, ocr_text), 3),
            }
        )

    for seg in asr_segs:
        t0, t1 = float(seg.get("start") or 0), float(seg.get("end") or 0)
        raw = str(seg.get("text") or "").strip()
        if not raw or looks_like_hallucination(raw) or _cjk_len(raw) < 6:
            continue
        duration = float(full.get("duration") or 0)
        if duration and t0 >= duration - 0.05:
            continue
        overlap = sum(
            max(0.0, min(t1, float(item["end"])) - max(t0, float(item["start"])))
            for item in new_segs
        )
        if overlap > 0.05:
            continue
        nearest = min(
            (min(abs(t0 - float(item["end"])), abs(t1 - float(item["start"]))) for item in new_segs),
            default=99.0,
        )
        if nearest < 0.35:
            continue
        new_segs.append(
            {
                "id": 0,
                "start": round(t0, 3),
                "end": round(t1, 3),
                "text": raw,
                "text_asr": raw,
                "text_whisper": raw,
                "words": list(seg.get("words") or []),
                "source": "asr-extra",
            }
        )

    new_segs.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    for index, item in enumerate(new_segs, start=1):
        item["id"] = index
    out["segments"] = new_segs
    out["ocr_aligned"] = True
    return out


def correct_transcript_with_ocr(transcript: dict[str, Any], ocr: dict[str, Any] | None) -> dict[str, Any]:
    return finalize_transcript(transcript, ocr)


def _speech_source(seg: dict[str, Any]) -> str:
    raw = str(seg.get("source") or "").strip()
    if raw in {"ocr", "asr", "asr-extra"}:
        return raw
    if seg.get("corrected_by"):
        return "ocr"
    return "asr"


def _usable_line(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or looks_like_hallucination(raw):
        return False
    if _cjk_len(raw) < 2:
        return False
    return True


def _ocr_asr_cases(segments: list[dict[str, Any]]) -> tuple[list[int], list[int], list[dict[str, Any]]]:
    """返回：直接丢掉的下标、改用 ASR 正文的下标、需要 LLM 二选一的条目。"""
    drop: list[int] = []
    use_asr: list[int] = []
    judge: list[dict[str, Any]] = []
    for index, seg in enumerate(segments):
        if str(seg.get("source") or "") != "ocr":
            continue
        ocr = str(seg.get("text") or "").strip()
        asr = str(seg.get("text_asr") or seg.get("text_whisper") or "").strip()
        ocr_ok, asr_ok = _usable_line(ocr), _usable_line(asr)
        if not ocr_ok and not asr_ok:
            drop.append(index)
            continue
        if ocr_ok and not asr_ok:
            continue
        if asr_ok and not ocr_ok:
            use_asr.append(index)
            continue
        if _norm(ocr) == _norm(asr):
            continue
        judge.append({"index": index, "id": len(judge) + 1, "ocr": ocr, "asr": asr})
    return drop, use_asr, judge


def needs_ocr_asr_judge(transcript: dict[str, Any]) -> bool:
    _drop, _use_asr, judge = _ocr_asr_cases(list(transcript.get("segments") or []))
    return bool(judge)


def adjudicate_ocr_asr(
    settings: Any,
    transcript: dict[str, Any],
    *,
    log: Any | None = None,
) -> dict[str, Any]:
    """OCR 和 ASR 都像人话但文字不一致时，让 LLM 原样二选一。"""
    segs = list(transcript.get("segments") or [])
    duration = float(transcript.get("duration") or 0)
    extra_drop = [
        index
        for index, seg in enumerate(segs)
        if str(seg.get("source") or "") == "asr-extra"
        and (
            not _usable_line(str(seg.get("text") or ""))
            or (duration and float(seg.get("start") or 0) >= duration - 0.05)
        )
    ]
    drop, use_asr, judge = _ocr_asr_cases(segs)
    drop = sorted(set(drop) | set(extra_drop))
    for index in use_asr:
        asr = str(segs[index].get("text_asr") or segs[index].get("text_whisper") or "").strip()
        segs[index]["text"] = asr
        segs[index]["source"] = "asr"
        segs[index]["corrected_by"] = "asr-over-ocr"
    picks: dict[int, str] = {}
    if judge:
        from vrs.llmclient import generate_text
        from vrs.textjson import parse_json_payload

        rows = "\n".join(
            f"{item['id']}. OCR「{item['ocr']}」 ASR「{item['asr']}」" for item in judge
        )
        prompt = (
            "下面每一条都是同一时刻的两种转写：画面烧字（OCR）和语音识别（ASR）。\n"
            "请判断哪一句更像人口里实际说的中文。必须原样采用其中一句，不许改写、不许合成第三句。\n\n"
            f"{rows}\n\n"
            '只输出 JSON：{"picks":[{"id":1,"use":"ocr"}]}\n'
            "use 只能是 ocr 或 asr。"
        )
        try:
            raw = generate_text(settings, prompt)
            payload = parse_json_payload(raw, require="picks")
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"OCR/ASR 裁判失败，不一致的句子保留 OCR：{exc}")
            payload = {}
        rows_out = payload.get("picks") if isinstance(payload, dict) else payload
        if isinstance(payload, dict) and not rows_out and isinstance(payload.get("choose"), list):
            rows_out = payload.get("choose")
        for item in rows_out or []:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            use = str(item.get("use") or item.get("choose") or "").strip().lower()
            if use in {"ocr", "asr"}:
                picks[pid] = use
        if log:
            log(f"OCR/ASR 裁判 {len(judge)} 句，模型回了 {len(picks)} 条")
        by_id = {item["id"]: item for item in judge}
        for pid, use in picks.items():
            hit = by_id.get(pid)
            if not hit:
                continue
            seg = segs[hit["index"]]
            if use == "asr":
                seg["text"] = hit["asr"]
                seg["source"] = "asr"
                seg["corrected_by"] = "llm-asr"
            else:
                seg["corrected_by"] = "llm-ocr"
        for item in judge:
            if item["id"] in picks:
                continue
            # 模型没点名的，有烧字就留 OCR
            segs[item["index"]]["corrected_by"] = "ocr-keep"
    keep = [seg for i, seg in enumerate(segs) if i not in set(drop)]
    for index, item in enumerate(keep, start=1):
        item["id"] = index
    out = dict(transcript)
    out["segments"] = keep
    return out


def merge_dialogue(transcript: dict[str, Any], ocr: dict[str, Any]) -> dict[str, Any]:
    speech = [
        {
            "t0": s["start"],
            "t1": s["end"],
            "text": s["text"],
            "source": _speech_source(s),
            "vocal_emotion": s.get("vocal_emotion"),
        }
        for s in transcript.get("segments") or []
        if (s.get("text") or "").strip()
    ]
    on_screen = _group_ocr(ocr.get("items") or [], float(ocr.get("fps") or 2.0))
    aligned: list[dict[str, Any]] = []
    used_ocr: set[int] = set()
    for spoken in speech:
        best_i, best_score = -1, 0.0
        for index, seen in enumerate(on_screen):
            if index in used_ocr:
                continue
            overlap = min(spoken["t1"], seen["t1"]) - max(spoken["t0"], seen["t0"])
            if overlap < 0.12:
                continue
            score = _similar(spoken["text"], seen["text"])
            if score > best_score:
                best_i, best_score = index, score
        if best_i >= 0 and best_score >= 0.55:
            seen = on_screen[best_i]
            used_ocr.add(best_i)
            aligned.append(
                {
                    "t0": spoken["t0"],
                    "t1": spoken["t1"],
                    "speech": spoken["text"],
                    "on_screen": seen["text"],
                    "region": seen["region"],
                    "corrected": seen["text"],
                    "score": round(best_score, 3),
                }
            )
        else:
            aligned.append(
                {
                    "t0": spoken["t0"],
                    "t1": spoken["t1"],
                    "speech": spoken["text"],
                    "on_screen": None,
                    "region": None,
                    "corrected": spoken["text"],
                    "score": 0.0,
                }
            )
    return {"speech": speech, "on_screen": on_screen, "aligned": aligned}


def sentence_ends(dialogue: dict[str, Any]) -> list[float]:
    ends: list[float] = []
    for item in dialogue.get("aligned") or []:
        ends.append(float(item["t1"]))
    for item in dialogue.get("on_screen") or []:
        ends.append(float(item["t1"]))
    return sorted(set(round(t, 3) for t in ends))
