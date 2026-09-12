from __future__ import annotations

import base64
import gc
import json
import re
from pathlib import Path
from typing import Any

import httpx

from vrs.settings import Settings

VIDEO_PROMPT = (
    "你在做镜像级视频翻拍的分镜理解，输出要能直接写 H3 脚本。"
    "禁止概括整段；必须按给定的 shot_id 逐条写，一条镜头一条对象。"
    "不要转写字幕或口播（OCR/ASR 负责）。"
    "只输出 JSON 数组，每项字段："
    "shot_id, t0, t1, setting, time_of_day, characters "
    "(name_or_role, age_look, wardrobe, hair, blocking, facing, emotion, hands),"
    "camera (shot_size, angle, movement, lens_feel), action_beats (按时间顺序的短句),"
    "props, lighting, color, transition_in, notes。"
    "动作写具体位移和互动，不要空话。"
)

FRAMES_PROMPT = (
    "这些是同一镜头按时间顺序的关键帧（首/中/尾）。"
    "为镜像翻拍写可执行的外观说明，不要读字幕。"
    "只输出 JSON，字段：shot_id, characters "
    "(role, sex, age_look, face, hair, wardrobe_layers, footwear, accessories),"
    "blocking, eyeline, props, composition (framing, subject_position, depth),"
    "lighting (source, direction, color, contrast), continuity_risks。"
)

# 写稿前对 HD 单帧做一次细读。横条分辨率不够，不能在拍表里做这种扫描。
# 不采用多轮刷维度、情绪打分、开尔文、伦勃朗光、抄字幕——那些会让 H3 烧字或演过火。
LOOK_PROMPT = """\
你在给 MiniMax H3 复刻写读图笔记。输入是同一条片段按时间顺序的 3 张单帧（开头、中间、结尾）。
只写看见的。不要写剧本，不要判定谁在说话，不要抄画面上的字和徽章。

第一张是本条开场。人在翻墙、跳跃、跨越、悬在障碍物上时：body_in_frame 写成全身或头到脚，shot_size 写成中景或全景，path.body 写清越过障碍再到落地，不要收成已经站在地上。
只有地点或人物组相对后两张变了，才丢掉第一张（那才是上一段末帧）。三张都在同一场就三张都用。
只记贯穿本场的主体。后景路人、车上的人写进 space，不要进 people。
开场不是翻越/跳跃时，有对白的主体才按胸口以上写笔记。

只输出一个 JSON 对象：
{
  "people": [
    {
      "where": "画面左前景/中/右中景",
      "body_in_frame": "额到上胸|头到腰|全身|仅面部",
      "face": "眉（平/蹙/一侧抬）；眼（开合、朝向）；嘴（闭/微张/张、是否露齿）；颌（松/咬紧）",
      "head": "朝向（正对/3/4侧/侧）；俯仰（平/微仰/微俯）",
      "hands": "位置、手型、指尖朝哪、是否碰到谁或什么",
      "body": "躯干朝向、前倾或后靠、肩线",
      "wardrobe": "发型、每一层衣服颜色款式、手里东西"
    }
  ],
  "camera": {
    "shot_size": "近景|中近景|中景|全景",
    "angle": "平视|微仰|微俯",
    "motion": "固定|手持微晃|缓慢推近|横移",
    "motion_evidence": "三帧里背景怎么移、人物怎么占画"
  },
  "light": "主光从哪来、硬或软、暖或冷，哪半边脸更亮",
  "space": "室内或室外、地点、地面、背景近/中/远各有什么、主要道具。字和徽章写成不可读色块",
  "path": {
    "face": "第1帧脸→第2帧→第3帧",
    "hands": "手的运动轨迹",
    "body": "身体位移"
  },
  "mouth": ["第1帧嘴型", "第2帧", "第3帧"]
}
禁止：情绪 1-10 分、真实/表演判断、色温开尔文、伦勃朗/蝴蝶光、抄字幕、品牌名。
看不清就写「看不清」，不要编。
"""
LOOK_REV = 3

DEFAULT_VL_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
CURSOR_SDK_KIND = "cursor_sdk"
# 这两种 kind 都由 sdkclient.generate_text 承接（内部再分 backend）
_SDK_KINDS = {"cursor_sdk", "anthropic_sdk"}
_THINK = re.compile(r"<think>.*?</think>", re.S)
_SESSION: dict[str, Any] = {"model": None, "processor": None, "key": None}


class VLError(RuntimeError):
    pass


def resolve_vl(settings: Settings) -> dict[str, Any]:
    cfg = dict(settings.providers.get("vl") or {})
    cfg.setdefault("kind", CURSOR_SDK_KIND)
    cfg.setdefault("model", DEFAULT_VL_MODEL)
    return cfg


def _model_id(cfg: dict[str, Any]) -> str:
    return str(cfg.get("model") or DEFAULT_VL_MODEL).strip() or DEFAULT_VL_MODEL


_SHARD_BYTES = {
    "model-00001-of-00004.safetensors": 4_902_275_944,
    "model-00002-of-00004.safetensors": 4_915_962_496,
    "model-00003-of-00004.safetensors": 4_999_831_048,
    "model-00004-of-00004.safetensors": 2_716_270_024,
}


def vl_weights_ready(root: Path) -> bool:
    """config.json 不够：分片要下完（无 .aria2），体积还得对上。"""
    index = root / "model.safetensors.index.json"
    if not (root / "config.json").is_file() or not index.is_file():
        return False
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    shards = {str(name) for name in (payload.get("weight_map") or {}).values()}
    if not shards:
        shards = set(_SHARD_BYTES)
    for name in shards:
        path = root / name
        if (root / f"{name}.aria2").is_file():
            return False
        if not path.is_file():
            return False
        expected = _SHARD_BYTES.get(name)
        if expected is not None and path.stat().st_size != expected:
            return False
        if expected is None and path.stat().st_size < 1_000_000:
            return False
    return True


def ensure_vl_weights(settings: Settings) -> Path:
    cfg = resolve_vl(settings)
    root = settings.path("qwen3_vl_model_dir")
    root.mkdir(parents=True, exist_ok=True)
    if vl_weights_ready(root):
        return root
    if any(root.glob("model-*.safetensors")) or any(root.glob("*.aria2")):
        raise VLError(f"Qwen3-VL-8B 分片未下完：{root}（不要改去 HuggingFace 重拉）")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise VLError("未安装 huggingface-hub，请执行 uv sync --extra vl") from exc
    snapshot_download(_model_id(cfg), local_dir=str(root))
    if not vl_weights_ready(root):
        raise VLError(f"Qwen3-VL-8B 分片未下完：{root}")
    return root


def vl_health(settings: Settings) -> tuple[bool, str]:
    cfg = resolve_vl(settings)
    kind = str(cfg.get("kind") or CURSOR_SDK_KIND).strip() or CURSOR_SDK_KIND
    model = _model_id(cfg)
    low = model.lower()
    if "-2b" in low or ":2b" in low or low.endswith("/2b"):
        return False, "VL 必须是 Qwen3-VL-8B-Instruct，不能用 2B"
    if "11434" in str(cfg.get("base_url") or ""):
        return False, "不要把 VL 指到 Ollama（吃不了视频，也不是 8B Transformers）"

    if kind in _SDK_KINDS:
        from vrs.sdkclient import sdk_health

        return sdk_health(settings, section="vl")

    if kind == "openai_compat":
        base = str(cfg.get("base_url") or "").rstrip("/")
        if not base:
            return False, "openai_compat 未配置 vl.base_url"
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.get(base + "/models")
            if response.status_code >= 500:
                return False, f"{base} HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "timeout" in text.lower() or "timed out" in text.lower():
                text = "连接超时"
            return False, f"{base} {text}"
        return True, f"{base} model={model}"

    try:
        import transformers  # noqa: F401
    except ImportError:
        return False, "未安装 transformers，请执行 uv sync --extra vl"

    root = settings.path("qwen3_vl_model_dir")
    if vl_weights_ready(root):
        return True, f"transformers {model} @ {root}"
    return False, (
        f"缺少 Qwen3-VL-8B 完整权重。放到 {root}（HuggingFace {model}），"
        "用 Transformers 加载；不要用 Ollama 2B。"
    )


def unload_vl() -> None:
    _SESSION["model"] = None
    _SESSION["processor"] = None
    _SESSION["key"] = None
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _headers(cfg: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = str(cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _local_path(path: Path) -> str:
    return str(path.resolve())


def _image_data_url(path: Path) -> str:
    payload = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def _strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _load_tf(settings: Settings) -> tuple[Any, Any]:
    cfg = resolve_vl(settings)
    root = ensure_vl_weights(settings)
    key = f"{root}:{_model_id(cfg)}:{settings.default.get('vl_quant') or 'auto'}"
    if _SESSION["model"] is not None and _SESSION["key"] == key:
        return _SESSION["model"], _SESSION["processor"]
    unload_vl()
    try:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError as exc:
        raise VLError("未安装 torch/transformers，请执行 uv sync --extra vl") from exc

    quant = str(settings.default.get("vl_quant") or "auto")
    load_kw: dict[str, Any] = {"device_map": "auto"}
    if torch.cuda.is_available():
        load_kw["dtype"] = torch.float16
        load_kw["attn_implementation"] = "sdpa"
    else:
        load_kw["dtype"] = torch.float32

    if quant in {"auto", "4bit"}:
        try:
            import bitsandbytes  # noqa: F401
            from transformers import BitsAndBytesConfig

            load_kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
        except Exception:
            if quant == "4bit":
                raise VLError("vl_quant=4bit 但 bitsandbytes 不可用")
            if torch.cuda.is_available():
                load_kw["max_memory"] = {0: "14GiB", "cpu": "48GiB"}
    elif torch.cuda.is_available():
        load_kw["max_memory"] = {0: "14GiB", "cpu": "48GiB"}

    try:
        processor = AutoProcessor.from_pretrained(str(root))
        model = AutoModelForImageTextToText.from_pretrained(str(root), **load_kw)
    except Exception as exc:  # noqa: BLE001
        if "quantization_config" in load_kw:
            load_kw.pop("quantization_config", None)
            if torch.cuda.is_available():
                load_kw["max_memory"] = {0: "14GiB", "cpu": "48GiB"}
            processor = AutoProcessor.from_pretrained(str(root))
            model = AutoModelForImageTextToText.from_pretrained(str(root), **load_kw)
        else:
            raise VLError(f"无法加载 Qwen3-VL-8B：{exc}") from exc
    model.eval()
    _SESSION["model"] = model
    _SESSION["processor"] = processor
    _SESSION["key"] = key
    return model, processor


def _tf_device(model: Any) -> Any:
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


def _shrink_vl_image(src: Path, dest: Path, max_edge: int) -> str:
    from PIL import Image

    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        scale = max_edge / max(width, height)
        if scale < 1:
            rgb = rgb.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.BILINEAR)
        rgb.save(dest, format="JPEG", quality=85, optimize=True)
    return _local_path(dest)


def _sample_clip_jpegs(clip: Path, count: int, *, max_edge: int) -> list[str]:
    from vrs.media import extract_frame_at
    from vrs.probe import probe_video

    duration = float(probe_video(clip)["duration"])
    n = max(4, int(count))
    if n % 2:
        n += 1
    folder = clip.parent / f"{clip.stem}_frames"
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index in range(n):
        raw = folder / f"{index:02d}.jpg"
        small = folder / f"{index:02d}_vl.jpg"
        if not raw.is_file():
            extract_frame_at(clip, raw, (index + 0.5) / n * duration)
        if max_edge > 0:
            paths.append(_shrink_vl_image(raw, small, max_edge))
        else:
            paths.append(_local_path(raw))
    return paths


def _tf_generate(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    num_frames: int | None = None,
    max_new_tokens: int | None = None,
) -> str:
    model, processor = _load_tf(settings)
    if num_frames:
        kw_tries: list[dict[str, Any] | None] = [
            {
                "num_frames": int(num_frames),
                "do_sample_frames": True,
                "videos_kwargs": {"cap_pixels_per_frame": True},
            },
            {"num_frames": int(num_frames), "do_sample_frames": True},
            None,
        ]
    else:
        kw_tries = [None]
    tokens = int(max_new_tokens or settings.default.get("vl_max_new_tokens") or 900)
    template_error: Exception | None = None
    inputs = None
    for pkw in kw_tries:
        apply_kw: dict[str, Any] = {
            "tokenize": True,
            "add_generation_prompt": True,
            "return_dict": True,
            "return_tensors": "pt",
        }
        if pkw:
            apply_kw["processor_kwargs"] = pkw
        try:
            inputs = processor.apply_chat_template(messages, **apply_kw)
            break
        except TypeError as exc:
            template_error = exc
            inputs = None
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "unexpected keyword" in text or "processor_kwargs" in text:
                template_error = exc
                inputs = None
                continue
            raise VLError(str(exc)) from exc
    if inputs is None:
        raise VLError(str(template_error) if template_error else "VL 无法编码输入")
    try:
        inputs = inputs.to(_tf_device(model))
        generated = model.generate(
            **inputs,
            max_new_tokens=tokens,
            do_sample=False,
            repetition_penalty=float(settings.default.get("vl_repetition_penalty") or 1.1),
            no_repeat_ngram_size=int(settings.default.get("vl_no_repeat_ngram") or 24),
        )
        trimmed = [out[len(inp) :] for inp, out in zip(inputs.input_ids, generated)]
        text = processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        text = _strip_think(text)
        if not text:
            raise VLError("VL 返回空内容")
        return text
    except Exception as exc:  # noqa: BLE001
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        raise VLError(str(exc)) from exc


def _cursor_analyze_images(
    settings: Settings,
    images: list[Path],
    prompt: str,
    *,
    max_new_tokens: int,
    max_edge: int | None = None,
) -> str:
    from vrs.sdkclient import SDKError, generate_text

    edge = int(max_edge if max_edge is not None else settings.default.get("vl_image_max_edge") or 1024)
    paths = [path for path in images if path.is_file()]
    if not paths:
        raise VLError("没有可读的图片")
    if edge > 0:
        prepared: list[Path] = []
        for index, path in enumerate(paths):
            dest = path.parent / "_vl" / f"cursor_{index:02d}_{path.name}"
            _shrink_vl_image(path, dest, edge)
            prepared.append(dest)
        paths = prepared
    stats: dict[str, Any] = {}
    try:
        return generate_text(
            settings,
            prompt,
            images=paths,
            max_new_tokens=max_new_tokens,
            stats=stats,
            section="vl",
        )
    except SDKError as exc:
        raise VLError(str(exc)) from exc


def _cursor_analyze_video(
    settings: Settings,
    clip: Path,
    *,
    t0: float,
    t1: float,
    shots: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    from vrs.sdkclient import sdk_model

    frames = int(settings.default.get("vl_video_num_frames") or 8)
    max_edge = int(settings.default.get("vl_image_max_edge") or 1024)
    from vrs.media import extract_frame_at
    from vrs.probe import probe_video

    duration = float(probe_video(clip)["duration"])
    lo = max(0.0, min(float(t0), duration))
    hi = max(lo + 0.05, min(float(t1) if t1 > 0 else duration, duration))
    if hi - lo < 0.05:
        lo, hi = 0.0, duration
    n = max(4, min(frames, 8))
    folder = clip.parent / f"{clip.stem}_cursor_{int(round(lo * 100)):05d}-{int(round(hi * 100)):05d}"
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(n):
        raw = folder / f"{index:02d}.jpg"
        stamp = lo + (index + 0.5) / n * (hi - lo)
        if not raw.is_file():
            extract_frame_at(clip, raw, stamp)
        paths.append(raw)
    prompt = (
        f"{VIDEO_PROMPT}\n时间范围 {t0:.2f}-{t1:.2f} 秒。\n{_shot_lines(shots)}\n"
        "以下图片按时间顺序对应该范围；必须逐张对应 shot_id，不要把相邻镜头合并。"
    )
    try:
        raw = _cursor_analyze_images(
            settings,
            paths,
            prompt,
            max_new_tokens=int(settings.default.get("vl_video_max_tokens") or 2400),
            max_edge=max_edge,
        )
        return {
            "ok": True,
            "mode": "cursor_frames",
            "raw": raw,
            "num_frames": len(paths),
            "model": sdk_model(settings, "vl"),
            "backend": "cursor_sdk",
        }
    except VLError as exc:
        return {"ok": False, "mode": "cursor_frames", "error": str(exc), "backend": "cursor_sdk"}


def _cursor_analyze_frames(
    settings: Settings,
    frames: list[Path],
    *,
    shot_id: str,
    t0: float,
    t1: float,
) -> dict[str, Any]:
    from vrs.sdkclient import sdk_model

    prompt = f"{FRAMES_PROMPT}\n镜头 {shot_id}，时间 {t0:.2f}-{t1:.2f} 秒。"
    try:
        raw = _cursor_analyze_images(
            settings,
            frames,
            prompt,
            max_new_tokens=int(settings.default.get("vl_frames_max_tokens") or 1200),
        )
        return {
            "ok": True,
            "mode": "cursor_frames",
            "raw": raw,
            "backend": "cursor_sdk",
            "model": sdk_model(settings, "vl"),
            "frames": [path.name for path in frames[:4]],
        }
    except VLError as exc:
        return {"ok": False, "mode": "cursor_frames", "error": str(exc), "backend": "cursor_sdk"}


def analyze_image(
    settings: Settings,
    image: Path,
    prompt: str,
    *,
    max_new_tokens: int | None = None,
) -> str:
    return analyze_images(settings, [image], prompt, max_new_tokens=max_new_tokens)


def analyze_images(
    settings: Settings,
    images: list[Path],
    prompt: str,
    *,
    max_new_tokens: int | None = None,
    max_edge: int | None = None,
) -> str:
    """多图问答。试片对照用：源片帧和草稿帧按顺序喂进去。"""
    paths = [path for path in images if path.is_file()]
    if not paths:
        raise VLError("没有可读的图片")
    tokens = int(max_new_tokens or settings.default.get("vl_beats_max_tokens") or 1800)
    edge = int(max_edge if max_edge is not None else settings.default.get("vl_look_max_edge") or 768)
    cfg = resolve_vl(settings)
    if str(cfg.get("kind") or "") in _SDK_KINDS:
        return _cursor_analyze_images(settings, paths, prompt, max_new_tokens=tokens, max_edge=edge)
    if str(cfg.get("kind") or "transformers") == "openai_compat":
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for frame in paths:
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
        return _chat(settings, content, timeout=float(settings.default.get("vl_timeout") or 180))
    prepared = _prepared_frame_paths(settings, paths, tag="compare", max_edge=edge)
    content = [{"type": "image", "image": image} for image in prepared]
    content.append({"type": "text", "text": prompt})
    return _tf_generate(
        settings,
        [{"role": "user", "content": content}],
        max_new_tokens=tokens,
    )


def _shot_lines(shots: list[dict[str, Any]] | None) -> str:
    if not shots:
        return ""
    lines = ["必须按下列镜头逐条描述，禁止把多镜合成一句："]
    for shot in shots:
        lines.append(
            f"- {shot.get('id')}  {float(shot.get('t0') or 0):.2f}-{float(shot.get('t1') or 0):.2f}s"
        )
    return "\n".join(lines)


def _tf_analyze_video(
    settings: Settings,
    clip: Path,
    *,
    t0: float,
    t1: float,
    shots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt = f"{VIDEO_PROMPT}\n时间范围 {t0:.2f}-{t1:.2f} 秒。\n{_shot_lines(shots)}"
    frames = int(settings.default.get("vl_video_num_frames") or 8)
    max_edge = int(settings.default.get("vl_image_max_edge") or 640)
    last_error = ""
    for count in (frames, max(4, frames // 2)):
        try:
            jpegs = _sample_clip_jpegs(clip, count, max_edge=max_edge)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "video", "video": jpegs},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            raw = _tf_generate(settings, messages, max_new_tokens=int(settings.default.get("vl_video_max_tokens") or 1200))
            cfg = resolve_vl(settings)
            return {
                "ok": True,
                "mode": "video",
                "raw": raw,
                "num_frames": len(jpegs),
                "model": _model_id(cfg),
                "backend": "transformers",
            }
        except VLError as exc:
            last_error = str(exc)
            if "out of memory" not in last_error.lower() and "oom" not in last_error.lower():
                break
    return {"ok": False, "mode": "video", "error": last_error}


def _prepared_frame_paths(
    settings: Settings, frames: list[Path], *, tag: str, max_edge: int | None = None
) -> list[str]:
    if max_edge is None:
        max_edge = int(settings.default.get("vl_image_max_edge") or 640)
    out: list[str] = []
    for index, path in enumerate(frames):
        if max_edge > 0:
            dest = path.parent / "_vl" / f"{tag}_{index:02d}.jpg"
            out.append(_shrink_vl_image(path, dest, max_edge))
        else:
            out.append(_local_path(path))
    return out


def _tf_analyze_frames(
    settings: Settings,
    frames: list[Path],
    *,
    shot_id: str,
    t0: float,
    t1: float,
) -> dict[str, Any]:
    prompt = f"{FRAMES_PROMPT}\n镜头 {shot_id}，时间 {t0:.2f}-{t1:.2f} 秒。"
    images = _prepared_frame_paths(settings, frames, tag=shot_id)
    content: list[dict[str, Any]] = [{"type": "image", "image": image} for image in images]
    content.append({"type": "text", "text": prompt})
    raw = _tf_generate(
        settings,
        [{"role": "user", "content": content}],
        max_new_tokens=int(settings.default.get("vl_frames_max_tokens") or 500),
    )
    cfg = resolve_vl(settings)
    return {
        "ok": True,
        "mode": "frames",
        "raw": raw,
        "backend": "transformers",
        "model": _model_id(cfg),
        "frames": [path.name for path in frames[:4]],
    }


def _tf_analyze_frames_batch(
    settings: Settings,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(items) == 1:
        item = items[0]
        return [
            _tf_analyze_frames(
                settings,
                item["paths"],
                shot_id=str(item["shot_id"]),
                t0=float(item["t0"]),
                t1=float(item["t1"]),
            )
        ]
    content: list[dict[str, Any]] = []
    lines = [FRAMES_PROMPT, "下面多组关键帧，按 shot_id 输出 JSON 数组，一组镜头一个对象。"]
    for item in items:
        images = _prepared_frame_paths(settings, item["paths"], tag=str(item["shot_id"]))
        for image in images:
            content.append({"type": "image", "image": image})
        lines.append(
            f"以上 {len(images)} 张对应 {item['shot_id']}（{float(item['t0']):.2f}-{float(item['t1']):.2f}s）。"
        )
    content.append({"type": "text", "text": "\n".join(lines)})
    raw = _tf_generate(
        settings,
        [{"role": "user", "content": content}],
        max_new_tokens=int(settings.default.get("vl_frames_max_tokens") or 500) * len(items),
    )
    cfg = resolve_vl(settings)
    shared = {
        "ok": True,
        "mode": "frames_batch",
        "raw": raw,
        "backend": "transformers",
        "model": _model_id(cfg),
        "batch_ids": [str(item["shot_id"]) for item in items],
    }
    return [{**shared, "frames": [path.name for path in item["paths"][:4]]} for item in items]


def _chat(settings: Settings, content: list[dict[str, Any]], *, timeout: float | None = None) -> str:
    cfg = resolve_vl(settings)
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "").strip()
    if not base:
        raise VLError("未配置 vl.base_url")
    wait = float(timeout if timeout is not None else settings.default.get("vl_chat_timeout") or 300)
    body: dict[str, Any] = {
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.2,
        "max_tokens": 2048,
        "stream": False,
        "think": False,
    }
    if model:
        body["model"] = model
    with httpx.Client(timeout=wait) as client:
        response = client.post(base + "/chat/completions", headers=_headers(cfg), json=body)
    if response.status_code >= 400:
        raise VLError(f"VL HTTP {response.status_code}: {response.text[:400]}")
    data = response.json()
    try:
        raw = str(data["choices"][0]["message"].get("content") or "")
        text = _strip_think(raw)
        if not text:
            raise VLError("VL 返回空内容")
        return text
    except VLError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VLError(f"VL 返回无法解析：{data!r}"[:400]) from exc


def analyze_video(
    settings: Settings,
    clip: Path,
    *,
    t0: float,
    t1: float,
    shots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = resolve_vl(settings)
    if str(cfg.get("kind") or "") in _SDK_KINDS:
        return _cursor_analyze_video(settings, clip, t0=t0, t1=t1, shots=shots)
    if str(cfg.get("kind") or "transformers") != "openai_compat":
        return _tf_analyze_video(settings, clip, t0=t0, t1=t1, shots=shots)
    text = f"{VIDEO_PROMPT}\n时间范围 {t0:.2f}-{t1:.2f} 秒。\n{_shot_lines(shots)}"
    try:
        content = [
            {"type": "text", "text": text},
            {"type": "video_url", "video_url": {"url": clip.resolve().as_uri()}},
        ]
        raw = _chat(settings, content)
        return {"ok": True, "mode": "video_url", "raw": raw}
    except VLError as exc:
        return {"ok": False, "mode": "video_url", "error": str(exc)}


def analyze_frames(
    settings: Settings,
    frames: list[Path],
    *,
    shot_id: str,
    t0: float,
    t1: float,
) -> dict[str, Any]:
    cfg = resolve_vl(settings)
    if str(cfg.get("kind") or "") in _SDK_KINDS:
        return _cursor_analyze_frames(settings, frames, shot_id=shot_id, t0=t0, t1=t1)
    if str(cfg.get("kind") or "transformers") != "openai_compat":
        return _tf_analyze_frames(settings, frames, shot_id=shot_id, t0=t0, t1=t1)
    text = f"{FRAMES_PROMPT}\n镜头 {shot_id}，时间 {t0:.2f}-{t1:.2f} 秒。"
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for frame in frames[:4]:
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
    raw = _chat(settings, content)
    return {
        "ok": True,
        "mode": "frames",
        "raw": raw,
        "endpoint": str(cfg.get("base_url") or ""),
        "model": str(cfg.get("model") or ""),
    }


def analyze_frames_batch(settings: Settings, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return []
    cfg = resolve_vl(settings)
    if str(cfg.get("kind") or "") in _SDK_KINDS:
        return [
            _cursor_analyze_frames(
                settings,
                item["paths"],
                shot_id=str(item["shot_id"]),
                t0=float(item["t0"]),
                t1=float(item["t1"]),
            )
            for item in items
        ]
    if str(cfg.get("kind") or "transformers") == "openai_compat":
        return [
            analyze_frames(
                settings,
                item["paths"],
                shot_id=str(item["shot_id"]),
                t0=float(item["t0"]),
                t1=float(item["t1"]),
            )
            for item in items
        ]
    return _tf_analyze_frames_batch(settings, items)


def look_frames(
    settings: Settings,
    frames: list[Path],
    *,
    clip_id: str,
    t0: float,
    t1: float,
) -> dict[str, Any]:
    """写稿用的 HD 三帧细读。横条拍表做不到这个颗粒度。"""
    paths = [p for p in frames if p.is_file()][:3]
    if not paths:
        return {"ok": False, "error": "没有可读的帧", "look_rev": LOOK_REV}
    prompt = (
        f"{LOOK_PROMPT}\n片段 {clip_id}，源片 {t0:.2f}-{t1:.2f}s，"
        f"下面 {len(paths)} 张按时间顺序：开头→中间→结尾。"
    )
    max_edge = int(settings.default.get("vl_look_max_edge") or 1024)
    tokens = int(settings.default.get("vl_look_max_tokens") or 2200)
    cfg = resolve_vl(settings)
    try:
        if str(cfg.get("kind") or "") in _SDK_KINDS:
            raw = _cursor_analyze_images(settings, paths, prompt, max_new_tokens=tokens, max_edge=max_edge)
        elif str(cfg.get("kind") or "transformers") == "openai_compat":
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for frame in paths:
                content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
            raw = _chat(settings, content)
        else:
            images = _prepared_frame_paths(settings, paths, tag=f"look_{clip_id}", max_edge=max_edge)
            content = [{"type": "image", "image": image} for image in images]
            content.append({"type": "text", "text": prompt})
            raw = _tf_generate(settings, [{"role": "user", "content": content}], max_new_tokens=tokens)
    except VLError as exc:
        return {"ok": False, "error": str(exc), "look_rev": LOOK_REV}
    return {
        "ok": True,
        "look_rev": LOOK_REV,
        "raw": raw,
        "model": _model_id(cfg) if str(cfg.get("kind") or "") != "openai_compat" else str(cfg.get("model") or ""),
        "frames": [path.name for path in paths],
    }
