from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from vrs.app import create_app
from vrs.envcheck import collect_env
from vrs.h3grid import GENERATE_PATHS
from vrs.jobstore import get_job, iter_jobs
from vrs.lock import BusyError
from vrs.metrics import collect_metrics
from vrs.runner import IngestGateError, cancel_job, create_and_download, resume_download
from vrs.settings import Settings


def _print_env(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload["summary"])
    gate = payload["gate_new_job"]
    print("新建任务门槛:", "通过" if gate["ok"] else "不通过 — " + "; ".join(gate["reasons"]))
    print("\n安装")
    for item in payload["install"]:
        mark = "OK" if item["ok"] else item["status"].upper()
        print(f"  [{mark}] {item['id']}: {item['detail']}")
    print("\n活服务")
    for item in payload["live"]:
        mark = "OK" if item["ok"] else item["status"].upper()
        print(f"  [{mark}] {item['id']}: {item['detail']}")


def _print_metrics(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    gpu = payload.get("gpu")
    if gpu:
        used = gpu.get("memory_used_mb")
        total = gpu.get("memory_total_mb")
        print(
            f"GPU {gpu.get('name')}  显存 {used}/{total} MB  "
            f"占用 {gpu.get('utilization_pct')}%  "
            f"温度 {gpu.get('temperature_c')}°C"
        )
        if payload["alerts"]["vram_hot"]:
            print("  显存 ≥90%（生成时发黄是预期）")
        if payload["alerts"]["temp_hot"]:
            print("  温度 ≥80°C")
    else:
        print("GPU —（读不到 nvidia-smi）")
    ram = payload["ram"]
    print(f"内存 {ram['used_bytes'] / (1024**3):.1f}/{ram['total_bytes'] / (1024**3):.1f} GB")
    h3 = payload["h3"]
    print(f"H3 {h3['label']}" + (f"  queue={h3['queue_length']}" if h3["queue_length"] else ""))


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(prog="vrs", description="Video Remake Studio")
    sub = parser.add_subparsers(dest="cmd", required=True)

    env_p = sub.add_parser("env", help="环境检查与设备指标")
    env_p.add_argument("--json", action="store_true")
    env_p.add_argument("--stage", default="idle")
    env_p.add_argument("--metrics-only", action="store_true")

    serve_p = sub.add_parser("serve", help="启动 FastAPI（默认 127.0.0.1:8787）")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)

    run_p = sub.add_parser("run", help="新建任务并执行下载")
    src = run_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="视频 URL（抖音走 f2，其余走 yt-dlp）")
    src.add_argument("--file", dest="file_path", help="本机绝对路径")
    run_p.add_argument("--review-mode", default=None)
    run_p.add_argument(
        "--path",
        dest="generate_path",
        default="i2va_turbo",
        choices=list(GENERATE_PATHS),
    )
    run_p.add_argument("--json", action="store_true")

    sub.add_parser("jobs", help="列出任务")

    resume_p = sub.add_parser("resume", help="从暂停或失败处续跑")
    resume_p.add_argument("job_id")
    resume_p.add_argument("--json", action="store_true")
    resume_p.add_argument(
        "--clips",
        dest="only_clips",
        default=None,
        help="只重写并生成这些 clip_id，逗号分隔，例如 h3_02,h3_03,h3_04",
    )

    cancel_p = sub.add_parser("cancel", help="放弃正在跑的任务（打断 Comfy，保留产物）")
    cancel_p.add_argument("job_id")

    approve_p = sub.add_parser("approve-script", help="预检通过后出试片（与 resume 相同）")
    approve_p.add_argument("job_id")
    approve_p.add_argument("--json", action="store_true")

    show_p = sub.add_parser("show", help="查看单个任务")
    show_p.add_argument("job_id")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.cmd == "env":
        env_payload = collect_env(settings, stage=args.stage)
        metrics_payload = collect_metrics(settings)
        if args.metrics_only:
            _print_metrics(metrics_payload, as_json=args.json)
        elif args.json:
            print(
                json.dumps(
                    {"env": env_payload, "metrics": metrics_payload},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _print_env(env_payload, as_json=False)
            print()
            _print_metrics(metrics_payload, as_json=False)
        return 0

    if args.cmd == "serve":
        host = args.host or settings.bind_host()
        port = args.port or settings.bind_port()
        uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
        return 0

    if args.cmd == "run":
        try:
            job = create_and_download(
                settings,
                url=args.url,
                file_path=args.file_path,
                review_mode=args.review_mode,
                generate_path=args.generate_path,
            )
        except (IngestGateError, BusyError) as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            print(f"{job['id']}  {job['state']}  stage={job['stage']}")
            probe = (job.get("source") or {}).get("probe") or {}
            if probe:
                print(
                    f"  {probe.get('width')}x{probe.get('height')}  "
                    f"{probe.get('duration'):.2f}s  {job['source']['video']}"
                )
            if job.get("note"):
                print(job["note"])
            pm = (job.get("stages") or {}).get("pagemeta") or {}
            if pm.get("status"):
                print(f"  页面元信息: {pm['status']}" + (f" ({pm['error']})" if pm.get("error") else ""))
            und = (job.get("stages") or {}).get("understand") or {}
            if und.get("status"):
                print(f"  理解: {und['status']}" + (f" ({und['error']})" if und.get("error") else ""))
            err = (job.get("stages") or {}).get("download", {}).get("error")
            if err:
                print(err, file=sys.stderr)
        return 0 if job.get("state") != "failed" else 1

    if args.cmd == "jobs":
        for job in iter_jobs(settings):
            print(f"{job['id']}\t{job['state']}\t{job['stage']}\t{job['source']['kind']}")
        return 0

    if args.cmd == "show":
        try:
            _, job = get_job(settings, args.job_id)
        except FileNotFoundError:
            print("任务不存在", file=sys.stderr)
            return 2
        print(json.dumps(job, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "resume":
        try:
            job = resume_download(
                settings,
                args.job_id,
                only_clips_arg=[p.strip() for p in args.only_clips.split(",") if p.strip()]
                if args.only_clips
                else None,
            )
        except FileNotFoundError:
            print("任务不存在", file=sys.stderr)
            return 2
        except (IngestGateError, BusyError) as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            print(f"{job['id']}  {job['state']}  stage={job['stage']}")
            if job.get("note"):
                print(job["note"])
            und = (job.get("stages") or {}).get("understand") or {}
            if und.get("status"):
                print(f"  理解: {und['status']}" + (f" ({und['error']})" if und.get("error") else ""))
            pre = (job.get("stages") or {}).get("precheck") or {}
            if pre.get("status"):
                print(f"  预检: {pre['status']}" + (f" ({pre['error']})" if pre.get("error") else ""))
            gen = (job.get("stages") or {}).get("generate") or {}
            if gen.get("status") and gen.get("status") != "pending":
                print(f"  生成: {gen['status']}" + (f" ({gen['error']})" if gen.get("error") else ""))
            fin = (job.get("stages") or {}).get("finish") or {}
            if fin.get("status") and fin.get("status") != "pending":
                print(f"  成片: {fin['status']}" + (f" ({fin['error']})" if fin.get("error") else ""))
        return 0 if job.get("state") != "failed" else 1

    if args.cmd == "cancel":
        try:
            job = cancel_job(settings, args.job_id)
        except FileNotFoundError:
            print("任务不存在", file=sys.stderr)
            return 2
        print(f"{job['id']}  {job['state']}  {job.get('note') or ''}")
        return 0

    if args.cmd == "approve-script":
        args.cmd = "resume"
        args.only_clips = None
        # fall through via recursive main would be messy; call resume
        try:
            job = resume_download(settings, args.job_id)
        except FileNotFoundError:
            print("任务不存在", file=sys.stderr)
            return 2
        except (IngestGateError, BusyError) as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(job, ensure_ascii=False, indent=2))
        else:
            print(f"{job['id']}  {job['state']}  stage={job['stage']}")
            if job.get("note"):
                print(job["note"])
        return 0 if job.get("state") != "failed" else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
