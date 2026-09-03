"""ComfyUI HTTP client：把 minmaxH3 的 UI 工作流转成 /prompt，逐段出片。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from vrs.settings import Settings

SKIP_TYPES = {"MarkdownNote", "Note", "Reroute"}
CONTROL_AFTER = {"fixed", "increment", "decrement", "randomize"}
UPLOAD_EXTRA = {"image", "video", "folder"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3"}

ASPECT_LABELS = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}


class ComfyError(RuntimeError):
    pass


class ComfyBusyError(ComfyError):
    pass


def base_url(settings: Settings) -> str:
    return str(settings.providers.get("comfy", {}).get("base_url") or "http://127.0.0.1:8188").rstrip("/")


def health(settings: Settings, *, timeout: float = 2.0) -> tuple[bool, str]:
    url = base_url(settings) + "/system_stats"
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
        if response.status_code < 500:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def wait_until_up(settings: Settings, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, _ = health(settings, timeout=2.0)
        if ok:
            return True
        time.sleep(2.0)
    return False


def start_comfy(settings: Settings, *, log_path: Path | None = None) -> None:
    cmd = str(settings.paths.get("comfy_start_cmd") or "").strip()
    if not cmd:
        raise ComfyError("未配置 comfy_start_cmd")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8")
        handle.write(f"$ {cmd}\n")
        handle.flush()
    else:
        handle = subprocess.DEVNULL
    kwargs: dict[str, Any] = {
        "args": cmd,
        "cwd": str(settings.root),
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "shell": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(**kwargs)


def interrupt(settings: Settings, prompt_id: str | None = None) -> None:
    payload: dict[str, Any] = {}
    if prompt_id:
        payload["prompt_id"] = prompt_id
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(base_url(settings) + "/interrupt", json=payload)
    except Exception:  # noqa: BLE001
        pass


def object_info(settings: Settings, *, cache: dict[str, Any] | None = None) -> dict[str, Any]:
    if cache is not None and cache.get("info"):
        return cache["info"]
    with httpx.Client(timeout=60.0) as client:
        response = client.get(base_url(settings) + "/object_info")
        response.raise_for_status()
        info = response.json()
    if not isinstance(info, dict):
        raise ComfyError("/object_info 不是 JSON 对象")
    if cache is not None:
        cache["info"] = info
    return info


def upload_image(settings: Settings, path: Path, *, subfolder: str = "") -> str:
    if not path.is_file():
        raise ComfyError(f"找不到首帧 {path}")
    with path.open("rb") as handle, httpx.Client(timeout=60.0) as client:
        response = client.post(
            base_url(settings) + "/upload/image",
            files={"image": (path.name, handle, "image/jpeg")},
            data={"overwrite": "true", "type": "input", "subfolder": subfolder},
        )
        response.raise_for_status()
        payload = response.json()
    name = str(payload.get("name") or path.name)
    sub = str(payload.get("subfolder") or "")
    return f"{sub}/{name}".replace("\\", "/").lstrip("/") if sub else name


def _view_bytes(settings: Settings, item: dict[str, Any]) -> bytes:
    params = {
        "filename": item.get("filename"),
        "subfolder": item.get("subfolder") or "",
        "type": item.get("type") or "output",
    }
    with httpx.Client(timeout=300.0) as client:
        response = client.get(base_url(settings) + "/view", params=params)
        response.raise_for_status()
        return response.content


def _history_videos(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node_out in (outputs or {}).values():
        if not isinstance(node_out, dict):
            continue
        for key in ("videos", "gifs", "images", "files"):
            for item in node_out.get(key) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("filename") or "")
                ext = Path(name).suffix.lower()
                if key == "videos" or ext in VIDEO_EXT:
                    found.append(item)
    return found


def _status_error(entry: dict[str, Any]) -> str | None:
    status = entry.get("status") or {}
    if str(status.get("status_str") or "") == "error" or any(
        isinstance(item, (list, tuple)) and item and item[0] == "execution_error"
        for item in status.get("messages") or []
    ):
        for item in status.get("messages") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], dict):
                msg = item[1].get("exception_message") or item[1].get("message")
                if msg:
                    return str(msg).strip()
            elif isinstance(item, dict):
                msg = item.get("exception_message") or item.get("message")
                if msg:
                    return str(msg).strip()
        return "Comfy 执行失败"
    return None


def _history_done(entry: dict[str, Any]) -> bool:
    status = entry.get("status") or {}
    if status.get("completed"):
        return True
    if str(status.get("status_str") or "") == "error":
        return True
    return any(
        isinstance(item, (list, tuple)) and item and item[0] == "execution_error"
        for item in status.get("messages") or []
    )


def wait_history(
    settings: Settings,
    prompt_id: str,
    *,
    timeout: float,
    abort: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    url = base_url(settings) + f"/history/{prompt_id}"
    while time.monotonic() < deadline:
        if abort and abort():
            interrupt(settings, prompt_id)
            raise ComfyError("已取消")
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
        except ComfyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ComfyError(f"读 history 失败：{exc}") from exc
        entry = payload.get(prompt_id) if isinstance(payload, dict) else None
        if isinstance(entry, dict) and _history_done(entry):
            err = _status_error(entry)
            if err:
                raise ComfyError(err)
            return entry
        time.sleep(2.0)
    interrupt(settings, prompt_id)
    raise ComfyError(f"生成超时（{int(timeout)}s）")


def download_output(settings: Settings, history: dict[str, Any], dest: Path) -> Path:
    videos = _history_videos(history.get("outputs") or {})
    if not videos:
        raise ComfyError("Comfy 没有视频输出")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_view_bytes(settings, videos[0]))
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        raise ComfyError("下载的视频太小，多半没生成成功")
    return dest


def _spec_type(spec: Any) -> Any:
    if isinstance(spec, (list, tuple)) and spec:
        return spec[0]
    return spec


def _spec_opts(spec: Any) -> dict[str, Any]:
    if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1]
    return {}


def _is_widget(spec: Any) -> bool:
    typ = _spec_type(spec)
    extra = _spec_opts(spec)
    if extra.get("forceInput"):
        return False
    if isinstance(typ, list):
        return True
    return str(typ) in WIDGET_TYPES


def _ui_input(node: dict[str, Any], name: str) -> dict[str, Any] | None:
    for item in node.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("name") or "")
        if raw == name or raw.endswith("." + name) or name.endswith("." + raw):
            return item
    return None


def _links_by_id(workflow: dict[str, Any]) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = {}
    for item in workflow.get("links") or []:
        if isinstance(item, (list, tuple)) and item:
            out[int(item[0])] = list(item)
    return out


def _follow(
    origin_id: int,
    origin_slot: int,
    nodes: dict[int, dict[str, Any]],
    links: dict[int, list[Any]],
) -> tuple[str, int]:
    node = nodes.get(origin_id)
    while node is not None and str(node.get("type") or "") == "Reroute":
        inp = (node.get("inputs") or [{}])[0]
        link_id = inp.get("link") if isinstance(inp, dict) else None
        if link_id is None:
            break
        row = links.get(int(link_id))
        if not row or len(row) < 3:
            break
        origin_id, origin_slot = int(row[1]), int(row[2])
        node = nodes.get(origin_id)
    return str(origin_id), origin_slot


def _input_names(info: dict[str, Any]) -> list[tuple[str, Any]]:
    order = info.get("input_order") or {}
    mapping = info.get("input") or {}
    names: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for group in ("required", "optional"):
        listed = order.get(group) if isinstance(order.get(group), list) else list((mapping.get(group) or {}).keys())
        bucket = mapping.get(group) or {}
        for name in listed:
            if name in seen:
                continue
            seen.add(name)
            names.append((str(name), bucket.get(name)))
    return names


def ui_to_api(workflow: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    nodes = {int(n["id"]): n for n in workflow.get("nodes") or [] if isinstance(n, dict) and "id" in n}
    links = _links_by_id(workflow)
    prompt: dict[str, Any] = {}
    missing: list[str] = []
    for node in nodes.values():
        class_type = str(node.get("type") or "")
        if class_type in SKIP_TYPES:
            continue
        if int(node.get("mode") or 0) in {2, 4}:
            continue
        meta = info.get(class_type)
        if not isinstance(meta, dict):
            missing.append(class_type)
            continue
        widgets = list(node.get("widgets_values") or [])
        widget_i = 0
        inputs: dict[str, Any] = {}
        for name, spec in _input_names(meta):
            ui_inp = _ui_input(node, name)
            link_id = ui_inp.get("link") if ui_inp else None
            if link_id is not None:
                row = links.get(int(link_id))
                if row and len(row) >= 3:
                    src, slot = _follow(int(row[1]), int(row[2]), nodes, links)
                    inputs[name] = [src, slot]
                if ui_inp and ui_inp.get("widget") and widget_i < len(widgets):
                    widget_i += 1
                    if widget_i < len(widgets) and widgets[widget_i] in CONTROL_AFTER:
                        widget_i += 1
                continue
            if not _is_widget(spec):
                continue
            if widget_i >= len(widgets):
                continue
            value = widgets[widget_i]
            widget_i += 1
            extra = _spec_opts(spec)
            if extra.get("control_after_generate") and widget_i < len(widgets) and widgets[widget_i] in CONTROL_AFTER:
                widget_i += 1
            if extra.get("image_upload") and widget_i < len(widgets) and widgets[widget_i] in UPLOAD_EXTRA:
                widget_i += 1
            inputs[name] = value
        for ui_inp in node.get("inputs") or []:
            if not isinstance(ui_inp, dict) or ui_inp.get("link") is None:
                continue
            name = str(ui_inp.get("name") or "")
            if not name or name in inputs:
                continue
            row = links.get(int(ui_inp["link"]))
            if not row or len(row) < 3:
                continue
            src, slot = _follow(int(row[1]), int(row[2]), nodes, links)
            inputs[name] = [src, slot]
        prompt[str(node["id"])] = {"class_type": class_type, "inputs": inputs}
    if missing:
        uniq = sorted(set(missing))
        raise ComfyError("Comfy 不认识这些节点：" + "、".join(uniq[:8]))
    return prompt


def aspect_label(raw: str) -> str:
    text = str(raw or "16:9").strip()
    if "(" in text:
        return text
    return ASPECT_LABELS.get(text, ASPECT_LABELS.get(text.replace(" ", ""), "16:9 (Widescreen)"))


def _title(node: dict[str, Any]) -> str:
    return str(node.get("title") or "")


def patch_ui_workflow(
    workflow: dict[str, Any],
    *,
    prompt: str,
    seconds: float,
    steps: int,
    megapixels: float,
    aspect: str,
    seed: int,
    filename_prefix: str,
    image_name: str | None = None,
    low_vram: bool | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
) -> dict[str, Any]:
    """改 UI 工作流的 widgets_values，再交给 ui_to_api。"""
    doc = json.loads(json.dumps(workflow))
    image_set = False
    for node in doc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("type") or "")
        widgets = node.get("widgets_values")
        title = _title(node).lower()
        if class_type in {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}:
            node["widgets_values"] = [prompt] + list(widgets[1:] if isinstance(widgets, list) else [])
        elif class_type == "PrimitiveStringMultiline" and "prompt" in title:
            node["widgets_values"] = [prompt]
        elif class_type == "PrimitiveFloat" and "duration" in title:
            node["widgets_values"] = [float(seconds)]
        elif class_type == "PrimitiveInt" and "step" in title:
            node["widgets_values"] = [int(steps)]
        elif class_type == "BasicScheduler" and isinstance(widgets, list) and len(widgets) >= 2:
            if scheduler:
                widgets[0] = scheduler
            widgets[1] = int(steps)
            node["widgets_values"] = widgets
        elif class_type == "ResolutionSelector" and isinstance(widgets, list) and len(widgets) >= 2:
            widgets[0] = aspect_label(aspect)
            widgets[1] = float(megapixels)
            node["widgets_values"] = widgets
        elif class_type == "RandomNoise" and isinstance(widgets, list) and widgets:
            widgets[0] = int(seed)
            if len(widgets) > 1:
                widgets[1] = "fixed"
            else:
                widgets.append("fixed")
            node["widgets_values"] = widgets
        elif class_type == "SaveVideo" and isinstance(widgets, list) and widgets:
            widgets[0] = filename_prefix
            node["widgets_values"] = widgets
        elif class_type == "LoadImage" and image_name and not image_set:
            node["widgets_values"] = [image_name, "image"]
            image_set = True
        elif class_type == "MiniMaxH3TurboLoRA" and isinstance(widgets, list) and len(widgets) >= 3 and low_vram is not None:
            widgets[2] = bool(low_vram)
            node["widgets_values"] = widgets
        elif class_type == "KSamplerSelect" and sampler:
            node["widgets_values"] = [sampler]
    return doc


def queue_prompt(settings: Settings, prompt: dict[str, Any], *, client_id: str) -> str:
    prompt_id = str(uuid.uuid4())
    body = {"prompt": prompt, "client_id": client_id, "prompt_id": prompt_id}
    with httpx.Client(timeout=60.0) as client:
        response = client.post(base_url(settings) + "/prompt", json=body)
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = {"error": response.text[:500]}
        err = payload.get("error") if isinstance(payload, dict) else payload
        extra = payload.get("node_errors") if isinstance(payload, dict) else None
        raise ComfyError(f"提交工作流失败：{err}" + (f" {extra}" if extra else ""))
    data = response.json()
    return str(data.get("prompt_id") or prompt_id)


def queue_busy(settings: Settings) -> bool:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(base_url(settings) + "/queue")
            response.raise_for_status()
            payload = response.json()
    except Exception:  # noqa: BLE001
        return False
    return bool(payload.get("queue_running") or payload.get("queue_pending"))


def load_workflow(settings: Settings, filename: str) -> dict[str, Any]:
    path = settings.path("minmax_h3_root") / "workflows" / filename
    if not path.is_file():
        raise ComfyError(f"找不到工作流 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("nodes"):
        raise ComfyError(f"工作流不是 UI JSON：{path}")
    return data


def run_h3_clip(
    settings: Settings,
    *,
    workflow: dict[str, Any],
    info: dict[str, Any],
    prompt_text: str,
    seconds: float,
    steps: int,
    megapixels: float,
    aspect: str,
    seed: int,
    filename_prefix: str,
    dest: Path,
    timeout: float,
    image_path: Path | None = None,
    image_subfolder: str = "",
    low_vram: bool | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    abort: Callable[[], bool] | None = None,
) -> tuple[str, Path]:
    image_name = None
    if image_path is not None:
        image_name = upload_image(settings, image_path, subfolder=image_subfolder)
    patched = patch_ui_workflow(
        workflow,
        prompt=prompt_text,
        seconds=seconds,
        steps=steps,
        megapixels=megapixels,
        aspect=aspect,
        seed=seed,
        filename_prefix=filename_prefix,
        image_name=image_name,
        low_vram=low_vram,
        sampler=sampler,
        scheduler=scheduler,
    )
    api = ui_to_api(patched, info)
    client_id = str(uuid.uuid4())
    prompt_id = queue_prompt(settings, api, client_id=client_id)
    history = wait_history(settings, prompt_id, timeout=timeout, abort=abort)
    download_output(settings, history, dest)
    return prompt_id, dest


def looks_like_oom(message: str) -> bool:
    text = message.lower()
    return any(
        token in text
        for token in (
            "out of memory",
            "outofmemory",
            "cuda oom",
            "cuda error",
            "hip error",
            "allocation on device",
        )
    )
