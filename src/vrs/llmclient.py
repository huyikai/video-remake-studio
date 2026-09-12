from __future__ import annotations

import gc
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vrs.settings import Settings
from vrs.textjson import parse_json_payload, strip_think
from vrs.vlclient import unload_vl

DEFAULT_LLM = "Qwen/Qwen3.5-9B"
SDK_KIND = "cursor_sdk"
# 这两种 kind 都由 sdkclient.generate_text 承接（内部再分 backend）
_SDK_KINDS = {"cursor_sdk", "anthropic_sdk"}
_SESSION: dict[str, Any] = {"model": None, "tokenizer": None, "key": None}


class LLMError(RuntimeError):
    pass


def resolve_llm(settings: Settings) -> dict[str, Any]:
    cfg = dict(settings.providers.get("llm") or {})
    cfg.setdefault("kind", "transformers")
    cfg.setdefault("model", DEFAULT_LLM)
    return cfg


def _kind(settings: Settings) -> str:
    cfg = resolve_llm(settings)
    return str(cfg.get("kind") or "transformers").strip() or "transformers"


def _model_id(cfg: dict[str, Any]) -> str:
    return str(cfg.get("model") or DEFAULT_LLM).strip() or DEFAULT_LLM


def llm_label(settings: Settings) -> str:
    kind = _kind(settings)
    if kind in _SDK_KINDS:
        from vrs.sdkclient import sdk_model

        return f"{kind}:{sdk_model(settings)}"
    return _model_id(resolve_llm(settings))


def llm_weights_ready(root: Path) -> bool:
    if not (root / "config.json").is_file():
        return False
    if any(root.glob("*.aria2")) or any(root.glob("*.incomplete")):
        return False
    index = root / "model.safetensors.index.json"
    if index.is_file():
        try:
            payload = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        names = {str(name) for name in (payload.get("weight_map") or {}).values()}
        if not names:
            return False
        for name in names:
            path = root / name
            if not path.is_file() or path.stat().st_size < 1_000_000:
                return False
        return True
    shards = [p for p in root.glob("*.safetensors") if p.is_file() and p.stat().st_size > 1_000_000]
    return bool(shards)


def ensure_llm_weights(settings: Settings) -> Path:
    cfg = resolve_llm(settings)
    root = settings.path("qwen3_5_model_dir")
    root.mkdir(parents=True, exist_ok=True)
    if llm_weights_ready(root):
        return root
    if any(root.glob("*.safetensors")) or any(root.glob("*.aria2")):
        raise LLMError(f"Qwen3.5-9B 分片未下完：{root}")
    model_id = _model_id(cfg)
    last: Exception | None = None
    try:
        from modelscope.hub.snapshot_download import snapshot_download

        snapshot_download(model_id=model_id, local_dir=str(root))
    except Exception as exc:  # noqa: BLE001
        last = exc
        try:
            from huggingface_hub import snapshot_download as hf_dl

            hf_dl(model_id, local_dir=str(root))
        except Exception as hf_exc:  # noqa: BLE001
            raise LLMError(f"无法下载 {model_id}：{hf_exc}") from hf_exc
    if not llm_weights_ready(root):
        extra = f"（modelscope: {last}）" if last else ""
        raise LLMError(f"Qwen3.5-9B 分片未下完：{root}{extra}")
    return root


def llm_health(settings: Settings) -> tuple[bool, str]:
    cfg = resolve_llm(settings)
    kind = str(cfg.get("kind") or "transformers").strip() or "transformers"
    if kind in _SDK_KINDS:
        from vrs.sdkclient import sdk_health

        return sdk_health(settings)
    model = _model_id(cfg)
    low = model.lower()
    if "11434" in str(cfg.get("base_url") or ""):
        return False, "文本 LLM 不要走 Ollama 11434；用 Transformers 加载 Qwen3.5-9B"
    if kind == "openai_compat":
        return False, "v1 文本 LLM 必须是本机 Transformers Qwen3.5-9B"
    try:
        import transformers  # noqa: F401
    except ImportError:
        return False, "未安装 transformers，请执行 uv sync --extra vl"
    root = settings.path("qwen3_5_model_dir")
    if llm_weights_ready(root):
        return True, f"transformers {model} @ {root}"
    return False, f"缺少 Qwen3.5-9B 完整权重。放到 {root}（HuggingFace {model}）。"


def unload_llm() -> None:
    _SESSION["model"] = None
    _SESSION["tokenizer"] = None
    _SESSION["key"] = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _bnb_config(quant: str, torch: Any) -> dict[str, Any]:
    from transformers import BitsAndBytesConfig

    compute = torch.float16 if torch.cuda.is_available() else torch.float32
    if quant == "8bit":
        return {"quantization_config": BitsAndBytesConfig(load_in_8bit=True)}
    return {
        "quantization_config": BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=compute,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
    }


def _load_tf(settings: Settings) -> tuple[Any, Any]:
    cfg = resolve_llm(settings)
    root = ensure_llm_weights(settings)
    quant = str(settings.default.get("llm_quant") or "auto")
    key = f"{root}:{_model_id(cfg)}:{quant}"
    if _SESSION["model"] is not None and _SESSION["key"] == key:
        return _SESSION["model"], _SESSION["tokenizer"]
    unload_vl()
    unload_llm()
    try:
        import torch
        from transformers import AutoTokenizer, Qwen3_5ForCausalLM
    except ImportError as exc:
        raise LLMError("未安装 torch/transformers，请执行 uv sync --extra vl") from exc

    if not torch.cuda.is_available():
        raise LLMError("Qwen3.5-9B 需要 CUDA，拒绝 CPU 推理")
    tokenizer = AutoTokenizer.from_pretrained(str(root), trust_remote_code=True)
    load_kw: dict[str, Any] = {
        "device_map": {"": 0},
        "trust_remote_code": True,
        "dtype": torch.float16,
    }
    try:
        import bitsandbytes  # noqa: F401
    except ImportError as exc:
        raise LLMError("llm_quant 需要 bitsandbytes（uv sync --extra vl）") from exc
    order = ["4bit", "8bit"] if quant in {"auto", "4bit"} else [quant if quant == "8bit" else "4bit", "8bit"]
    last: Exception | None = None
    model = None
    seen: list[str] = []
    for q in order:
        if q in seen:
            continue
        seen.append(q)
        try:
            model = Qwen3_5ForCausalLM.from_pretrained(str(root), **load_kw, **_bnb_config(q, torch))
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            model = None
    if model is None:
        raise LLMError(f"无法加载 Qwen3.5-9B：{last}")
    dmap = getattr(model, "hf_device_map", None) or {}
    devs = {str(v) for v in dmap.values()} if dmap else {str(next(model.parameters()).device)}
    if not any("cuda" in d for d in devs):
        unload_llm()
        raise LLMError(f"Qwen3.5-9B 未上 GPU（device={devs}），已拒绝 CPU 推理")
    model.eval()
    _SESSION["model"] = model
    _SESSION["tokenizer"] = tokenizer
    _SESSION["key"] = key
    return model, tokenizer


def _device(model: Any) -> Any:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.device("cuda")
    except Exception:
        pass
    try:
        return model.device
    except Exception:
        return next(model.parameters()).device


def generate_text(
    settings: Settings,
    prompt: str,
    *,
    thinking: bool | None = None,
    max_new_tokens: int | None = None,
    images: Sequence[Path] | None = None,
    stats: dict[str, Any] | None = None,
) -> str:
    if _kind(settings) in _SDK_KINDS:
        from vrs.sdkclient import SDKError
        from vrs.sdkclient import generate_text as sdk_generate

        try:
            return sdk_generate(
                settings,
                prompt,
                thinking=thinking,
                max_new_tokens=max_new_tokens,
                images=images,
                stats=stats,
            )
        except SDKError as exc:
            raise LLMError(str(exc)) from exc
    if images:
        raise LLMError("本机 Qwen3.5-9B 是纯文本模型，看不了图；带图请把 llm.kind 设为 cursor_sdk 或 anthropic_sdk")
    model, tokenizer = _load_tf(settings)
    think = bool(settings.default.get("llm_enable_thinking") if thinking is None else thinking)
    tokens = int(max_new_tokens or settings.default.get("llm_max_new_tokens") or 8192)
    messages = [{"role": "user", "content": prompt}]
    try:
        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            enable_thinking=think,
        )
    except TypeError:
        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            chat_template_kwargs={"enable_thinking": think},
        )
    inputs = {k: v.to(_device(model)) if hasattr(v, "to") else v for k, v in inputs.items()}
    gen_kw: dict[str, Any] = {"max_new_tokens": tokens}
    if think:
        gen_kw.update(do_sample=True, temperature=0.6, top_p=0.95, top_k=20)
    else:
        gen_kw.update(do_sample=False)
    generated = model.generate(**inputs, **gen_kw)
    in_len = int(inputs["input_ids"].shape[-1])
    trimmed = generated[0][in_len:]
    if stats is not None:
        stats.update(model=_model_id(resolve_llm(settings)), tok_in=in_len, tok_out=int(trimmed.shape[-1]))
    if int(trimmed.shape[-1]) >= tokens:
        raise LLMError(f"输出到达 {tokens} token 上限被截断")
    text = tokenizer.decode(trimmed, skip_special_tokens=True)
    text = strip_think(text)
    if not text:
        raise LLMError("LLM 返回空内容")
    return text


def generate_json(settings: Settings, prompt: str, **kwargs: Any) -> Any:
    last: Exception | None = None
    for _ in range(2):
        try:
            raw = generate_text(settings, prompt, **kwargs)
            return parse_json_payload(raw)
        except (LLMError, ValueError, json.JSONDecodeError) as exc:
            last = exc
    raise LLMError(f"LLM JSON 无法解析：{last}")
