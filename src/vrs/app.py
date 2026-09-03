from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from vrs.envcheck import collect_env
from vrs.h3grid import GENERATE_PATHS
from vrs.jobops import (
    JobOpsError,
    confirm_aspect,
    delete_job,
    patch_settings,
    probe_local_file,
    save_all_json,
    save_clip_script,
    settings_public,
)
from vrs.jobstore import get_job
from vrs.jobview import (
    clip_editor_payload,
    job_detail,
    list_jobs_payload,
    occupied_job_id,
    our_comfy_context,
    resolve_job_file,
)
from vrs.lock import BusyError
from vrs.metrics import collect_metrics
from vrs.runner import (
    IngestGateError,
    cancel_job,
    create_and_download,
    rerun_drafts,
    run_finals,
)
from vrs.settings import Settings
from vrs.worker import spawn_resume


class JobCreate(BaseModel):
    url: str | None = None
    file_path: str | None = None
    review_mode: str | None = None
    generate_path: Literal["i2va_turbo", "t2va_turbo", "ref2va"] = "i2va_turbo"
    smtp: bool | None = None
    vl_mode: str | None = None
    aspect_ratio: str | None = None
    aspect_confirmed: bool = False

    @model_validator(mode="after")
    def one_source(self) -> JobCreate:
        if bool(self.url) == bool(self.file_path):
            raise ValueError("必须只提供 url 或 file_path 其中一个")
        return self


class ProbeBody(BaseModel):
    file_path: str


class AspectBody(BaseModel):
    follow_source: bool


class ClipSave(BaseModel):
    prompt_json: dict[str, Any] | None = None
    prompt_txt: str | None = None
    review_md: str | None = None
    h3_seconds: float | None = None


class ScriptSave(BaseModel):
    clips: dict[str, Any] | None = None
    by_clip: dict[str, Any] | None = None


class SettingsPatch(BaseModel):
    comfy_base_url: str | None = None
    gpu_memory_gb: float | None = None
    hang_timeout_sec: int | None = None
    generate_clip_timeout_sec: int | None = None
    clip_restart_max: int | None = None
    job_restart_max: int | None = None
    aspect_ratio: str | None = None
    aspect_confirm: bool | None = None
    review_mode: str | None = None
    vl_mode: str | None = None
    esrgan: bool | None = None
    ass_burn: bool | None = None
    smtp_enabled: bool | None = None
    smtp_to: list[str] | str | None = None


class DraftBody(BaseModel):
    clip_ids: list[str] = Field(default_factory=list)


def _http_busy(exc: BusyError) -> HTTPException:
    return HTTPException(409, str(exc))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Video Remake Studio", version="0.1.0")
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8787",
            "http://localhost:8787",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _settings() -> Settings:
        return app.state.settings

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/env")
    def api_env(stage: str = Query(default="idle")) -> dict:
        running = occupied_job_id(_settings())
        if running and stage == "idle":
            try:
                _, job = get_job(_settings(), running)
                stage = str(job.get("stage") or "idle")
            except FileNotFoundError:
                pass
        return collect_env(_settings(), stage=stage)

    @app.get("/api/env/metrics")
    def api_metrics() -> dict:
        ids, workflow, quality = our_comfy_context(_settings())
        return collect_metrics(
            _settings(),
            our_prompt_ids=ids,
            our_workflow=workflow,
            our_quality=quality,
        )

    @app.get("/api/settings")
    def api_settings() -> dict:
        return settings_public(_settings())

    @app.patch("/api/settings")
    def api_patch_settings(body: SettingsPatch) -> dict:
        try:
            return patch_settings(_settings(), body.model_dump(exclude_none=True))
        except JobOpsError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/probe")
    def api_probe(body: ProbeBody) -> dict:
        try:
            return probe_local_file(body.file_path)
        except JobOpsError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/jobs")
    def api_jobs() -> dict:
        return list_jobs_payload(_settings())

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict:
        try:
            return job_detail(_settings(), job_id)
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None

    @app.get("/api/jobs/{job_id}/clips/{clip_id}")
    def api_clip(job_id: str, clip_id: str) -> dict:
        try:
            directory, _ = get_job(_settings(), job_id)
            return clip_editor_payload(directory, clip_id)
        except FileNotFoundError:
            raise HTTPException(404, "任务或片段不存在") from None

    @app.get("/api/jobs/{job_id}/files/{rel_path:path}")
    def api_file(job_id: str, rel_path: str):
        try:
            directory, _ = get_job(_settings(), job_id)
            path = resolve_job_file(directory, rel_path)
        except FileNotFoundError:
            raise HTTPException(404, "文件不存在") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        mime, _ = mimetypes.guess_type(str(path))
        return FileResponse(path, media_type=mime or "application/octet-stream")

    @app.get("/api/jobs/{job_id}/events")
    async def api_events(job_id: str):
        async def gen():
            last = ""
            while True:
                try:
                    detail = job_detail(_settings(), job_id, compact=True)
                except FileNotFoundError:
                    yield "event: gone\ndata: {}\n\n"
                    break
                blob = json.dumps(detail, ensure_ascii=False, default=str)
                if blob != last:
                    yield f"data: {blob}\n\n"
                    last = blob
                await asyncio.sleep(1.0 if detail.get("running") else 2.5)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/jobs", status_code=201)
    def api_create_job(body: JobCreate) -> dict:
        if body.generate_path not in GENERATE_PATHS:
            raise HTTPException(400, "未知生成路线")
        try:
            job = create_and_download(
                _settings(),
                url=body.url,
                file_path=body.file_path,
                review_mode=body.review_mode,
                generate_path=body.generate_path,
                smtp=body.smtp,
                vl_mode=body.vl_mode,
                aspect_ratio=body.aspect_ratio,
                aspect_confirmed=body.aspect_confirmed,
                background=True,
            )
        except IngestGateError as exc:
            raise HTTPException(400, str(exc)) from exc
        except BusyError as exc:
            raise _http_busy(exc) from exc
        return job

    @app.post("/api/jobs/{job_id}/resume")
    def api_resume(job_id: str) -> dict:
        try:
            get_job(_settings(), job_id)
            if occupied_job_id(_settings()):
                raise BusyError(f"已有任务在跑：{occupied_job_id(_settings())}")
            spawn_resume(_settings(), job_id)
            _, job = get_job(_settings(), job_id)
            return job
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        except BusyError as exc:
            raise _http_busy(exc) from exc
        except IngestGateError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/draft")
    def api_draft(job_id: str, body: DraftBody | None = None) -> dict:
        try:
            if occupied_job_id(_settings()):
                raise BusyError(f"已有任务在跑：{occupied_job_id(_settings())}")
            ids = list((body or DraftBody()).clip_ids)

            def fn() -> None:
                rerun_drafts(_settings(), job_id, ids or None)

            from vrs.worker import spawn

            spawn(_settings(), job_id, fn)
            _, job = get_job(_settings(), job_id)
            return job
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        except (BusyError, IngestGateError) as exc:
            status = 409 if isinstance(exc, BusyError) else 400
            raise HTTPException(status, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/final")
    def api_final(job_id: str) -> dict:
        try:
            if occupied_job_id(_settings()):
                raise BusyError(f"已有任务在跑：{occupied_job_id(_settings())}")

            def fn() -> None:
                run_finals(_settings(), job_id)

            from vrs.worker import spawn

            spawn(_settings(), job_id, fn)
            _, job = get_job(_settings(), job_id)
            return job
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        except (BusyError, IngestGateError) as exc:
            status = 409 if isinstance(exc, BusyError) else 400
            raise HTTPException(status, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel(job_id: str) -> dict:
        try:
            return cancel_job(_settings(), job_id)
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None

    @app.post("/api/jobs/{job_id}/aspect")
    def api_aspect(job_id: str, body: AspectBody) -> dict:
        try:
            return confirm_aspect(_settings(), job_id, follow_source=body.follow_source)
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None

    @app.put("/api/jobs/{job_id}/clips/{clip_id}")
    def api_save_clip(job_id: str, clip_id: str, body: ClipSave) -> dict:
        try:
            return save_clip_script(
                _settings(),
                job_id,
                clip_id,
                prompt_json=body.prompt_json,
                prompt_txt=body.prompt_txt,
                review_md=body.review_md,
                h3_seconds=body.h3_seconds,
            )
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        except JobOpsError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put("/api/jobs/{job_id}/script")
    def api_save_script(job_id: str, body: ScriptSave) -> dict:
        try:
            return save_all_json(
                _settings(),
                job_id,
                clips_doc=body.clips,
                by_clip=body.by_clip,
            )
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        except JobOpsError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/jobs/{job_id}")
    def api_delete(job_id: str) -> dict:
        try:
            delete_job(_settings(), job_id)
        except FileNotFoundError:
            raise HTTPException(404, "任务不存在") from None
        return {"ok": True}

    dist = settings.root / "web" / "dist"
    index = dist / "index.html"

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "not found")
        if dist.is_dir() and full_path:
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return {
            "app": "Video Remake Studio",
            "hint": "前端未构建。开发：pnpm --dir web dev；生产：pnpm --dir web build 后重启 vrs serve。",
            "api": "/api/env",
        }

    return app
