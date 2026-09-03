from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx

from vrs.pathsutil import assert_outside_repo, jobs_dir
from vrs.settings import Settings
from vrs.smtpcheck import check_smtp, smtp_enabled

STAGE_NEEDS: dict[str, list[str]] = {
    "idle": [],
    "download": ["ffmpeg", "yt-dlp"],
    "pagemeta": [],
    "understand": ["asr", "ocr", "ser", "vl", "llm"],
    "script": ["llm", "h3_skills"],
    "precheck": [],
    "generate": ["comfy", "h3_workflows"],
    "finish": ["ffmpeg"],
}


def _cmd_ok(name: str) -> tuple[bool, str]:
    found = shutil.which(name)
    if found:
        return True, found
    return False, f"PATH 中没有 {name}"


def _dir_nonempty(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _http_ok(url: str, timeout: float = 1.0) -> tuple[bool, str]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
        if response.status_code < 500:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _comfy_health(base_url: str) -> tuple[bool, str]:
    return _http_ok(base_url.rstrip("/") + "/system_stats")


def _openai_health(base_url: str) -> tuple[bool, str]:
    return _http_ok(base_url.rstrip("/") + "/models")


def _item(id_: str, ok: bool, detail: str, *, layer: str, needed: bool) -> dict[str, Any]:
    if ok:
        status = "ok"
    elif needed:
        status = "red"
    else:
        status = "yellow"
    return {
        "id": id_,
        "ok": ok,
        "detail": detail,
        "layer": layer,
        "needed_now": needed,
        "status": status,
    }


def collect_env(settings: Settings, *, stage: str = "idle") -> dict[str, Any]:
    stage = stage if stage in STAGE_NEEDS else "idle"
    needed = set(STAGE_NEEDS[stage])
    install: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []

    ffmpeg_ok, ffmpeg_detail = _cmd_ok("ffmpeg")
    install.append(_item("ffmpeg", ffmpeg_ok, ffmpeg_detail, layer="install", needed=True))

    ytdlp_ok, ytdlp_detail = _cmd_ok("yt-dlp")
    install.append(
        _item("yt-dlp", ytdlp_ok, ytdlp_detail, layer="install", needed="yt-dlp" in needed)
    )

    try:
        import f2  # noqa: F401

        f2_ok, f2_detail = True, "f2 已安装"
    except ImportError:
        f2_ok, f2_detail = False, "未安装 f2（抖音进料需要，uv sync）"
    install.append(_item("f2", f2_ok, f2_detail, layer="install", needed=False))

    from vrs.f2douyin import cookie_ready

    cookie_ok, cookie_detail = cookie_ready(settings)
    install.append(
        _item("douyin_cookie", cookie_ok, cookie_detail, layer="install", needed=False)
    )

    nvsmi_ok, nvsmi_detail = _cmd_ok("nvidia-smi")
    install.append(_item("nvidia_smi", nvsmi_ok, nvsmi_detail, layer="install", needed=False))

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

        pw_ok, pw_detail = True, "playwright 已安装"
    except ImportError:
        pw_ok, pw_detail = False, "未安装 playwright"
    install.append(_item("playwright", pw_ok, pw_detail, layer="install", needed=False))

    path_keys = [
        ("vrs_runtime_root", "vrs-runtime"),
        ("minmax_h3_root", "minmaxH3"),
        ("h3_skills_root", "h3-prompt-writing 根"),
        ("whisper_model_dir", "Whisper 权重（旧）"),
        ("ocr_model_dir", "OCR 权重"),
        ("qwen3_vl_model_dir", "Qwen3-VL-8B 权重"),
        ("qwen3_5_model_dir", "Qwen3.5-9B 权重"),
        ("qwen3_asr_model_dir", "Qwen3-ASR-1.7B 权重"),
        ("qwen3_aligner_model_dir", "Qwen3-ForcedAligner 权重"),
        ("emotion2vec_plus_large_dir", "emotion2vec+ large 权重"),
        ("realesrgan_dir", "RealESRGAN"),
    ]
    for key, label in path_keys:
        path = settings.path(key)
        inside = assert_outside_repo(label, path, settings)
        exists = path.exists()
        if inside:
            ok, detail = False, inside
        elif not exists:
            ok, detail = False, f"{label} 不存在：{path}"
        else:
            ok, detail = True, str(path)
        need_id = {
            "whisper_model_dir": None,
            "ocr_model_dir": "ocr",
            "qwen3_vl_model_dir": "vl",
            "qwen3_5_model_dir": "llm",
            "qwen3_asr_model_dir": "asr",
            "qwen3_aligner_model_dir": "asr",
            "emotion2vec_plus_large_dir": "ser",
            "h3_skills_root": "h3_skills",
            "minmax_h3_root": "h3_workflows",
        }.get(key)
        if key in {
            "ocr_model_dir",
            "qwen3_vl_model_dir",
            "qwen3_5_model_dir",
            "qwen3_asr_model_dir",
            "qwen3_aligner_model_dir",
            "emotion2vec_plus_large_dir",
        } and ok and not _dir_nonempty(path):
            ok, detail = False, f"{label} 目录为空：{path}"
        if key == "qwen3_asr_model_dir" and ok:
            from vrs.asr import asr_weights_ready

            if not asr_weights_ready(path):
                ok, detail = False, f"{label} 未下完：{path}"
        if key == "qwen3_aligner_model_dir" and ok:
            from vrs.asr import aligner_weights_ready

            if not aligner_weights_ready(path):
                ok, detail = False, f"{label} 未下完：{path}"
        if key == "emotion2vec_plus_large_dir" and ok:
            from vrs.ser import ser_weights_ready

            if not ser_weights_ready(path):
                ok, detail = False, f"{label} 未下完：{path}（需要 config.yaml、tokens.txt、model.pt）"
        if key == "qwen3_vl_model_dir" and ok:
            from vrs.vlclient import vl_weights_ready

            if not vl_weights_ready(path):
                ok, detail = False, f"{label} 分片未下完：{path}"
        if key == "qwen3_5_model_dir" and ok:
            from vrs.llmclient import llm_weights_ready

            if not llm_weights_ready(path):
                ok, detail = False, f"{label} 分片未下完：{path}"
        if key == "ocr_model_dir" and not ok:
            try:
                import rapidocr_onnxruntime

                bundled = Path(rapidocr_onnxruntime.__file__).resolve().parent / "models"
                if bundled.is_dir() and any(bundled.glob("*.onnx")):
                    ok, detail = True, f"首次运行将复制到 {path}"
            except Exception:
                pass
        install.append(
            _item(key, ok, detail, layer="install", needed=bool(need_id) and need_id in needed)
        )

    skills_root = settings.path("h3_skills_root")
    skill_file = skills_root / "h3-prompt-writing" / "SKILL.md"
    if not skill_file.is_file():
        alt = skills_root / "SKILL.md"
        skill_ok = alt.is_file()
        skill_detail = str(alt) if skill_ok else f"找不到 h3-prompt-writing/SKILL.md：{skill_file}"
        skill_file_used = alt if skill_ok else skill_file
    else:
        skill_ok, skill_detail, skill_file_used = True, str(skill_file), skill_file
    install.append(
        _item(
            "h3_prompt_writing",
            skill_ok,
            skill_detail,
            layer="install",
            needed="h3_skills" in needed,
        )
    )

    h3_root = settings.path("minmax_h3_root")
    workflows = [
        settings.h3.get("i2va_turbo", {}).get("workflow", "video_minimax_h3_i2v_turbo.json"),
        settings.h3.get("t2va_turbo", {}).get("workflow", "video_minimax_h3_t2v_turbo.json"),
        settings.h3.get("ref2va", {}).get("workflow", "video_minimax_h3_r2v.json"),
    ]
    missing_wf = []
    for name in workflows:
        candidate = h3_root / "workflows" / str(name)
        if not candidate.is_file():
            missing_wf.append(str(candidate))
    wf_ok = not missing_wf and h3_root.is_dir()
    wf_detail = "工作流齐全" if wf_ok else "缺少: " + "; ".join(missing_wf)
    install.append(
        _item("h3_workflows", wf_ok, wf_detail, layer="install", needed="h3_workflows" in needed)
    )

    start_ps1 = h3_root / "start.ps1"
    install.append(
        _item(
            "comfy_start",
            start_ps1.is_file() or bool(settings.paths.get("comfy_start_cmd")),
            str(start_ps1) if start_ps1.is_file() else str(settings.paths.get("comfy_start_cmd") or "未配置"),
            layer="install",
            needed=False,
        )
    )

    jobs = jobs_dir(settings)
    try:
        jobs.mkdir(parents=True, exist_ok=True)
        probe = jobs / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        jobs_ok, jobs_detail = True, str(jobs)
    except OSError as exc:
        jobs_ok, jobs_detail = False, str(exc)
    install.append(_item("jobs_writable", jobs_ok, jobs_detail, layer="install", needed=True))

    comfy_url = str(settings.providers.get("comfy", {}).get("base_url", "http://127.0.0.1:8188"))
    comfy_ok, comfy_detail = _comfy_health(comfy_url)
    live.append(
        _item(
            "comfy",
            comfy_ok,
            f"{comfy_url} {comfy_detail}",
            layer="live",
            needed="comfy" in needed,
        )
    )

    from vrs.vlclient import vl_health

    vl_ok, vl_detail = vl_health(settings)
    live.append(_item("vl", vl_ok, vl_detail, layer="live", needed="vl" in needed))

    from vrs.llmclient import llm_health

    llm_ok, llm_detail = llm_health(settings)
    live.append(_item("llm", llm_ok, llm_detail, layer="live", needed="llm" in needed))

    smtp = check_smtp(settings.smtp)
    smtp_needed = smtp_enabled(settings.smtp)
    live.append(
        _item(
            "smtp",
            bool(smtp["ok"]),
            str(smtp["detail"]),
            layer="live",
            needed=smtp_needed,
        )
    )

    gate_reasons: list[str] = []
    if not ffmpeg_ok:
        gate_reasons.append("缺少 ffmpeg")
    if not jobs_ok:
        gate_reasons.append("data/jobs 不可写")
    if smtp_needed and not smtp["ok"]:
        gate_reasons.append("SMTP 已启用但连接/登录失败；可关邮件后再新建")

    current_need = "无（空闲）" if stage == "idle" else "、".join(STAGE_NEEDS[stage]) or "无"
    idle_live = [item for item in live if item["id"] in {"comfy", "vl"} and not item["ok"]]
    summary = f"当前需要 {current_need}"
    if idle_live and stage == "idle":
        names = "、".join(item["id"] for item in idle_live)
        summary += f"；{names} 已配置、未运行（到该阶段再检查）"

    return {
        "stage": stage,
        "summary": summary,
        "install": install,
        "live": live,
        "gate_new_job": {"ok": not gate_reasons, "reasons": gate_reasons},
        "skill_file": str(skill_file_used),
        "auto_deploy": bool(settings.paths.get("auto_deploy")),
    }
