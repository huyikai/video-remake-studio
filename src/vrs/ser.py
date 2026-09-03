from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import yaml

from vrs.settings import Settings

DEFAULT_SER_MODEL = "emotion2vec/emotion2vec_plus_large"
MIN_UTTERANCE_SEC = 0.6
MIN_TOP1_SCORE = 0.45
NULL_LABELS = frozenset({"other", "unknown"})
# HF 上的 model.pt 约 1.95GB；未下完时体积会对不上。
_SER_WEIGHT_BYTES = 1_945_790_254

_EN_LABEL = {
    "angry": "angry",
    "disgusted": "disgusted",
    "fearful": "fearful",
    "happy": "happy",
    "neutral": "neutral",
    "other": "other",
    "sad": "sad",
    "surprised": "surprised",
    "unknown": "unknown",
    "<unk>": "unknown",
}


def _label_en(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text == "<unk>":
        return "unknown"
    if "/" in text:
        text = text.rsplit("/", 1)[-1].strip()
    return _EN_LABEL.get(text.lower(), text.lower())


def _read_labels(root: Path) -> list[str]:
    path = root / "tokens.txt"
    labels = [_label_en(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(labels) < 9:
        raise RuntimeError(f"emotion2vec tokens.txt 标签不足：{path}")
    return labels


def ser_weights_ready(root: Path) -> bool:
    weight = root / "model.pt"
    if (root / "model.pt.aria2").is_file():
        return False
    if not (root / "config.yaml").is_file():
        return False
    if not (root / "tokens.txt").is_file():
        return False
    if not weight.is_file():
        return False
    if weight.stat().st_size != _SER_WEIGHT_BYTES:
        return False
    try:
        labels = [
            _label_en(line)
            for line in (root / "tokens.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return False
    return len(labels) >= 9


def resolve_ser_model(settings: Settings, *, allow_missing: bool = False) -> Path:
    root = settings.path("emotion2vec_plus_large_dir")
    if ser_weights_ready(root):
        return root
    if allow_missing:
        raise FileNotFoundError(str(root))
    raise RuntimeError(
        f"找不到 emotion2vec+ large 权重。放到 {root}（需要 config.yaml、tokens.txt、model.pt）。"
        "用本机 PyTorch 加载，不要安装 FunASR / modelscope。"
        f" HuggingFace：{DEFAULT_SER_MODEL}"
    )


def _unwrap_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RuntimeError("emotion2vec model.pt 不是 state_dict")
    if "proj.weight" in raw or any(str(k).endswith("proj.weight") for k in raw):
        return raw
    for key in ("model", "state_dict", "model_state_dict"):
        inner = raw.get(key)
        if isinstance(inner, dict):
            return _unwrap_state(inner)
    return raw


def _strip_prefixes(state: dict[str, Any]) -> dict[str, Any]:
    prefixes = ("d2v_model.", "model.")
    out: dict[str, Any] = {}
    for key, value in state.items():
        name = str(key)
        changed = True
        while changed:
            changed = False
            for prefix in prefixes:
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                    changed = True
        out[name] = value
    if "proj.weight" not in out:
        for key, value in list(out.items()):
            if key.endswith("proj.weight"):
                out["proj.weight"] = value
            elif key.endswith("proj.bias"):
                out["proj.bias"] = value
    return out


def _load_net(root: Path, device: str):
    import torch

    from vrs.emotion2vec.cfg import wrap_cfg
    from vrs.emotion2vec.model import Emotion2vec

    cfg_raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    model_conf = wrap_cfg(cfg_raw.get("model_conf") or {})
    labels = _read_labels(root)
    net = Emotion2vec(model_conf, vocab_size=len(labels))
    try:
        raw = torch.load(str(root / "model.pt"), map_location="cpu", weights_only=True)
    except Exception:
        raw = torch.load(str(root / "model.pt"), map_location="cpu", weights_only=False)
    state = _strip_prefixes(_unwrap_state(raw))
    for name, module in net.named_modules():
        upgrade = getattr(module, "upgrade_state_dict_named", None)
        if callable(upgrade):
            upgrade(state, name)
    missing, unexpected = net.load_state_dict(state, strict=False)
    missing_now = [
        k
        for k in missing
        if not str(k).startswith("ema") and "decoder." not in str(k)
    ]
    if "proj.weight" in missing_now or net.proj is None:
        raise RuntimeError(f"emotion2vec 分类头未加载：missing={missing_now[:8]}")
    if missing_now:
        raise RuntimeError(f"emotion2vec 权重缺键：{missing_now[:12]}")
    net.eval()
    return net.to(device), labels


def _device() -> str:
    try:
        import torch

        from vrs.asr import _cublas_available

        if torch.cuda.is_available() and _cublas_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def classify_spans(
    wav: Path,
    spans: list[tuple[float, float]],
    settings: Settings,
) -> list[dict[str, Any] | None]:
    """对终审 [t0,t1] 切片做 utterance 分类。短于 0.6s / 低置信 / other|unknown → None。"""
    import torch

    from vrs.asr import _load_wav

    root = resolve_ser_model(settings)
    audio, sr = _load_wav(wav)
    device = _device()
    net = None
    try:
        net, labels = _load_net(root, device)
        out: list[dict[str, Any] | None] = []
        for t0, t1 in spans:
            dur = float(t1) - float(t0)
            if dur < MIN_UTTERANCE_SEC:
                out.append(None)
                continue
            i0 = max(0, int(round(float(t0) * sr)))
            i1 = min(len(audio), int(round(float(t1) * sr)))
            if i1 - i0 < int(MIN_UTTERANCE_SEC * sr):
                out.append(None)
                continue
            wave = torch.from_numpy(audio[i0:i1].copy()).to(device=device, dtype=torch.float32)
            with torch.inference_mode():
                probs = net.classify_utterance(wave)
            score, index = float(probs.max().item()), int(probs.argmax().item())
            label = labels[index] if 0 <= index < len(labels) else "unknown"
            if score < MIN_TOP1_SCORE or label in NULL_LABELS:
                out.append(None)
                continue
            out.append({"label": label, "score": round(score, 4)})
        return out
    finally:
        del net
        gc.collect()
        try:
            import torch as _torch

            if device == "cuda":
                _torch.cuda.empty_cache()
        except Exception:
            pass


def attach_vocal_emotion(
    dialogue: dict[str, Any],
    wav: Path,
    settings: Settings,
) -> dict[str, Any]:
    speech = list(dialogue.get("speech") or [])
    spans = [(float(item["t0"]), float(item["t1"])) for item in speech]
    emotions = classify_spans(wav, spans, settings)
    for item, emotion in zip(speech, emotions):
        item["vocal_emotion"] = emotion
    dialogue["speech"] = speech
    dialogue["ser"] = {
        "engine": "emotion2vec-plus-large",
        "model": DEFAULT_SER_MODEL,
        "source": str(resolve_ser_model(settings)),
    }
    return dialogue
