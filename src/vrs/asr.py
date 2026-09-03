from __future__ import annotations

import gc
import os
import re
import wave
from pathlib import Path
from typing import Any

from vrs.settings import Settings

_SPACE = re.compile(r"\s+")
_REPEAT_CHAR = re.compile(r"(.)\1{5,}")
_REPEAT_PAREN = re.compile(r"[)）]{4,}")
_PUNCT_END = "。！？!?，,、；;．."

DEFAULT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B-hf"
DEFAULT_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B-hf"
_ASR_WEIGHT_BYTES = 4_076_193_080
_ALIGNER_WEIGHT_BYTES = 1_835_545_960
_MAX_CHUNK_SEC = 240.0


def _phrase_loop(compact: str) -> bool:
    if len(compact) < 24:
        return False
    for size in range(8, 21):
        if len(compact) < size * 3:
            break
        if compact.count(compact[:size]) >= 3:
            return True
    if len(compact) >= 36:
        mid = compact[len(compact) // 4 : len(compact) // 4 + 12]
        if len(mid) == 12 and compact.count(mid) >= 3:
            return True
    return False


def looks_like_hallucination(text: str) -> bool:
    """静音/片尾/复读时的典型垃圾转写。"""
    t = (text or "").strip()
    if not t:
        return True
    if _REPEAT_PAREN.search(t) or _REPEAT_CHAR.search(t):
        return True
    compact = _SPACE.sub("", t)
    if len(compact) >= 8 and len(set(compact)) <= 2:
        return True
    if _phrase_loop(compact):
        return True
    cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    if latin >= 10 and cjk < 3:
        return True
    return False


def _dir_ready(root: Path, *, weight_name: str, expected: int) -> bool:
    weight = root / weight_name
    if (root / f"{weight_name}.aria2").is_file():
        return False
    if not (root / "config.json").is_file():
        return False
    if not (root / "tokenizer.json").is_file() or (root / "tokenizer.json").stat().st_size < 1_000_000:
        return False
    if not weight.is_file():
        return False
    size = weight.stat().st_size
    if expected and size != expected:
        return False
    return True


def asr_weights_ready(root: Path) -> bool:
    return _dir_ready(root, weight_name="model.safetensors", expected=_ASR_WEIGHT_BYTES)


def aligner_weights_ready(root: Path) -> bool:
    return _dir_ready(root, weight_name="model.safetensors", expected=_ALIGNER_WEIGHT_BYTES)


def resolve_asr_model(settings: Settings, *, allow_missing: bool = False) -> tuple[Path, str]:
    root = settings.path("qwen3_asr_model_dir")
    if asr_weights_ready(root):
        return root, str(root)
    if allow_missing:
        raise FileNotFoundError(str(root))
    raise RuntimeError(
        f"找不到 Qwen3-ASR-1.7B 权重。放到 {root}（需要 config.json、tokenizer.json、model.safetensors）。"
        "不要安装 qwen-asr 包，它会把 transformers 锁到 4.57，和 Qwen3-VL 冲突。"
    )


def resolve_aligner_model(settings: Settings, *, allow_missing: bool = False) -> tuple[Path, str]:
    root = settings.path("qwen3_aligner_model_dir")
    if aligner_weights_ready(root):
        return root, str(root)
    if allow_missing:
        raise FileNotFoundError(str(root))
    raise RuntimeError(
        f"找不到 Qwen3-ForcedAligner-0.6B 权重。放到 {root}（需要 config.json、tokenizer.json、model.safetensors）。"
    )


def transcript_stale(doc: dict[str, Any] | None, settings: Settings) -> bool:
    if not doc or not doc.get("segments"):
        return True
    wanted = str(settings.default.get("asr_model") or DEFAULT_ASR_MODEL).lower()
    short = wanted.rsplit("/", 1)[-1].lower()
    blob = f"{doc.get('source') or ''} {doc.get('model') or ''} {doc.get('engine') or ''}".lower()
    if "qwen3-asr" in blob or short in blob:
        return False
    return True


def _cublas_available() -> bool:
    try:
        import torch

        lib = Path(torch.__file__).resolve().parent / "lib"
        if (lib / "cublas64_12.dll").is_file():
            os.add_dll_directory(str(lib))
            os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")
            return True
    except Exception:
        pass
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and (Path(folder) / "cublas64_12.dll").is_file():
            return True
    return False


def _join_words(words: list[dict[str, Any]]) -> str:
    return _SPACE.sub(" ", "".join(str(w.get("word") or "") for w in words)).strip()


def _split_long_segments(segments: list[dict[str, Any]], *, max_dur: float = 8.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        words = list(seg.get("words") or [])
        dur = float(seg.get("end") or 0) - float(seg.get("start") or 0)
        if dur <= max_dur or len(words) < 4:
            out.append(seg)
            continue
        buckets: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        t0 = 0.0
        for word in words:
            if not current:
                t0 = float(word["start"])
            current.append(word)
            t1 = float(word["end"])
            token = str(word.get("word") or "").strip()[-1:]
            punct = token in _PUNCT_END
            if (t1 - t0 >= 3.5 and punct) or (t1 - t0 >= max_dur):
                buckets.append(current)
                current = []
        if current:
            if buckets and float(current[-1]["end"]) - float(current[0]["start"]) < 1.5:
                buckets[-1].extend(current)
            else:
                buckets.append(current)
        for bucket in buckets:
            out.append(
                {
                    "id": 0,
                    "start": round(float(bucket[0]["start"]), 3),
                    "end": round(float(bucket[-1]["end"]), 3),
                    "text": _join_words(bucket) or str(seg.get("text") or "").strip(),
                    "words": bucket,
                }
            )
    for index, item in enumerate(out, start=1):
        item["id"] = index
    return out


def _load_wav(path: Path) -> tuple[Any, int]:
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        sr = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"不支持的 wav 采样宽度 {width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if sr != 16000:
        n_out = int(round(len(data) * 16000 / sr))
        x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        data = np.interp(x_new, x_old, data).astype(np.float32)
        sr = 16000
    return data, sr


def _audio_chunks(audio, sr: int, *, max_sec: float = _MAX_CHUNK_SEC):
    max_n = int(max_sec * sr)
    if len(audio) <= max_n:
        yield 0.0, audio
        return
    hop = int((max_sec - 0.4) * sr)
    start = 0
    while start < len(audio):
        end = min(len(audio), start + max_n)
        yield start / sr, audio[start:end]
        if end >= len(audio):
            break
        start += hop


def _map_language(raw: str | None) -> str:
    text = (raw or "zh").strip()
    if not text or text.lower() in {"auto", "none"}:
        return "zh"
    return text


def transcribe_wav(
    wav: Path,
    settings: Settings,
    *,
    initial_prompt: str | None = None,
    hotwords: str | None = None,
) -> dict[str, Any]:
    try:
        import torch
        from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor
    except ImportError as exc:
        raise RuntimeError("未安装 transformers/torch，请执行 uv sync --extra asr --extra vl") from exc

    asr_dir, asr_source = resolve_asr_model(settings)
    aligner_dir, aligner_source = resolve_aligner_model(settings)
    language = _map_language(str(settings.default.get("asr_language") or settings.default.get("whisper_language") or "zh"))
    device_cfg = str(settings.default.get("asr_device") or settings.default.get("whisper_device") or "auto")
    max_new = int(settings.default.get("asr_max_new_tokens") or 1536)
    prompt = (hotwords or initial_prompt or "").strip() or None
    if prompt and len(prompt) > 400:
        prompt = prompt[:400]

    use_cuda = device_cfg == "cuda" or (device_cfg == "auto" and torch.cuda.is_available() and _cublas_available())
    device = "cuda" if use_cuda else "cpu"
    dtype = torch.bfloat16 if use_cuda else torch.float32

    audio, sr = _load_wav(wav)
    duration = round(len(audio) / max(sr, 1), 3)

    asr_processor = None
    asr_model = None
    aligner_processor = None
    aligner_model = None
    texts: list[str] = []
    words: list[dict[str, Any]] = []
    detected_lang = language
    try:
        asr_processor = AutoProcessor.from_pretrained(str(asr_dir), trust_remote_code=False)
        map_device = "cuda:0" if use_cuda else "cpu"
        asr_model = AutoModelForMultimodalLM.from_pretrained(
            str(asr_dir),
            dtype=dtype,
            device_map=map_device,
        )
        asr_model.eval()
        aligner_processor = AutoProcessor.from_pretrained(str(aligner_dir), trust_remote_code=False)
        aligner_model = AutoModelForTokenClassification.from_pretrained(
            str(aligner_dir),
            dtype=dtype,
            device_map=map_device,
        )
        aligner_model.eval()

        chunks = list(_audio_chunks(audio, sr))
        for index, (offset, chunk) in enumerate(chunks):
            # 传 numpy，不要传 wav 路径：transformers 5 会优先走 torchcodec，
            # Windows 上静态 FFmpeg 没有 DLL，会直接炸。
            inputs = asr_processor.apply_transcription_request(
                audio=chunk,
                sampling_rate=sr,
                language=language,
                prompt=prompt,
            )
            target_device = next(asr_model.parameters()).device
            target_dtype = next(asr_model.parameters()).dtype
            inputs = inputs.to(target_device, target_dtype)
            with torch.inference_mode():
                output_ids = asr_model.generate(**inputs, max_new_tokens=max_new)
            generated_ids = output_ids[:, inputs["input_ids"].shape[1] :]
            parsed = asr_processor.decode(generated_ids, return_format="parsed")[0]
            text = str((parsed or {}).get("transcription") or "").strip()
            if parsed and parsed.get("language"):
                detected_lang = str(parsed["language"])
            if not text or looks_like_hallucination(text):
                continue
            texts.append(text)
            aligner_inputs, word_lists = aligner_processor.prepare_forced_aligner_inputs(
                audio=chunk,
                transcript=text,
                language=detected_lang or language,
            )
            aligner_inputs = aligner_inputs.to(
                next(aligner_model.parameters()).device,
                next(aligner_model.parameters()).dtype,
            )
            with torch.inference_mode():
                outputs = aligner_model(**aligner_inputs)
            stamps = aligner_processor.decode_forced_alignment(
                logits=outputs.logits,
                input_ids=aligner_inputs["input_ids"],
                word_lists=word_lists,
                timestamp_token_id=aligner_model.config.timestamp_token_id,
            )[0]
            for item in stamps:
                words.append(
                    {
                        "word": str(item.get("text") or ""),
                        "start": round(float(item.get("start_time") or 0) + offset, 3),
                        "end": round(float(item.get("end_time") or 0) + offset, 3),
                    }
                )
    finally:
        del asr_model, asr_processor, aligner_model, aligner_processor
        gc.collect()
        if use_cuda:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    full_text = "".join(texts).strip()
    if not full_text:
        raise RuntimeError("Qwen3-ASR 没有识别出有效文本")
    return {
        "engine": "qwen3-asr",
        "model": Path(asr_dir).name,
        "aligner": Path(aligner_dir).name,
        "source": asr_source,
        "aligner_source": aligner_source,
        "device": device,
        "language": detected_lang,
        "duration": duration,
        "initial_prompt": prompt or "",
        "text": full_text,
        "words": words,
    }


# 旧名兼容：环境检查 / 理解阶段曾用 Whisper 解析器
def resolve_whisper_model(settings: Settings, *, allow_download: bool = True) -> tuple[str, str]:
    path, label = resolve_asr_model(settings)
    return str(path), label
