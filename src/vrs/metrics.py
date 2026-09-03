from __future__ import annotations

import shutil
import subprocess
from typing import Any

import httpx
import psutil

from vrs.settings import Settings

_NV_QUERY = (
    "name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw"
)


def _nvidia_smi() -> dict[str, Any] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        completed = subprocess.run(
            [
                exe,
                f"--query-gpu={_NV_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    line = completed.stdout.strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 6:
        return None

    def _num(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return None

    return {
        "name": parts[0],
        "memory_used_mb": _num(parts[1]),
        "memory_total_mb": _num(parts[2]),
        "utilization_pct": _num(parts[3]),
        "temperature_c": _num(parts[4]),
        "power_w": _num(parts[5]),
    }


def _comfy_queue(base_url: str) -> dict[str, Any]:
    empty = {"queue_length": 0, "prompt_id": None, "running": False, "reachable": False}
    try:
        with httpx.Client(timeout=1.0) as client:
            queue = client.get(base_url.rstrip("/") + "/queue")
            queue.raise_for_status()
            payload = queue.json()
    except Exception:  # noqa: BLE001
        return empty
    running = payload.get("queue_running") or []
    pending = payload.get("queue_pending") or []
    prompt_id = None
    if running:
        first = running[0]
        if isinstance(first, list) and len(first) > 1:
            prompt_id = str(first[1])
        elif isinstance(first, dict):
            prompt_id = str(first.get("prompt_id") or first.get("prompt") or "")
    return {
        "queue_length": len(running) + len(pending),
        "prompt_id": prompt_id or None,
        "running": bool(running),
        "reachable": True,
    }


def collect_metrics(
    settings: Settings,
    *,
    our_prompt_ids: set[str] | None = None,
    our_workflow: str | None = None,
    our_quality: str | None = None,
) -> dict[str, Any]:
    gpu = _nvidia_smi()
    mem = psutil.virtual_memory()
    if settings.mode() == "mock":
        return {
            "mode": "mock",
            "gpu": None,
            "ram": {"used_bytes": int(mem.used), "total_bytes": int(mem.total)},
            "h3": {
                "workflow_file": "mock://minimax-h3",
                "label": "Mock H3 执行器",
                "quality": "mock",
                "prompt_id": "mock-runner",
                "queue_length": 0,
                "foreign": False,
                "comfy_reachable": True,
            },
            "alerts": {"vram_hot": False, "temp_hot": False},
        }
    comfy_url = str(settings.providers.get("comfy", {}).get("base_url", "http://127.0.0.1:8188"))
    queue = _comfy_queue(comfy_url)
    ours = our_prompt_ids or set()
    foreign = False
    workflow_label = "未在生成"
    workflow_file = None
    quality = None
    if queue["running"] and queue["prompt_id"] and queue["prompt_id"] in ours:
        workflow_file = our_workflow
        workflow_label = our_workflow or "本任务生成中"
        quality = our_quality
    elif queue["queue_length"]:
        foreign = True
        workflow_label = "Comfy 占用中（外部）"
    return {
        "gpu": gpu,
        "ram": {
            "used_bytes": int(mem.used),
            "total_bytes": int(mem.total),
        },
        "h3": {
            "workflow_file": workflow_file,
            "label": workflow_label,
            "quality": quality,
            "prompt_id": queue["prompt_id"],
            "queue_length": queue["queue_length"],
            "foreign": foreign,
            "comfy_reachable": queue["reachable"],
        },
        "alerts": {
            "vram_hot": bool(
                gpu
                and gpu.get("memory_used_mb")
                and gpu.get("memory_total_mb")
                and gpu["memory_total_mb"] > 0
                and (gpu["memory_used_mb"] / gpu["memory_total_mb"]) >= 0.9
            ),
            "temp_hot": bool(gpu and gpu.get("temperature_c") is not None and gpu["temperature_c"] >= 80),
        },
    }
