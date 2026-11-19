from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from .extract import PDF_LOCK, crop_asset, rect_valid
from .jobs import JobManager, now
from .discovery import discover_models, test_connection
from .llm import LLMError
from .models import Asset, Document, Kind, LLMConfig, Model
from .package import pack, safe_path
from .structure import associate_assets, finalize, translation_numbers_valid
from .webdav import router as webdav_router

ROOT = Path(__file__).resolve().parent.parent


def create_app(data_dir: Path | None = None, access_token: str = "") -> FastAPI:
    manager = JobManager(data_dir or Path(os.getenv("PAPERLOOM_DATA_DIR", ROOT / ".data")))

    @asynccontextmanager
    async def lifespan(app):
        yield
        manager.close()

    app = FastAPI(title="Paperloom Preparation API", version="0.3.4", lifespan=lifespan)
    app.state.jobs = manager
    app.include_router(webdav_router)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            provided = request.headers.get("authorization", "").removeprefix("Bearer ")
            if access_token and not hmac.compare_digest(provided, access_token):
                return JSONResponse({"detail": "需要准备端访问令牌"}, status_code=401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                    return JSONResponse({"detail": "仅接受同源操作，请打开准备端自己的网页"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
        return response

    def directory(job_id):
        try:
            return manager.directory(job_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")

    def ready(job_id):
        path = directory(job_id)
        if manager.snapshot(job_id)["status"] != "ready":
            raise HTTPException(409, "任务尚未准备完成")
        return path

    def preparation_config(config):
        try:config.ensure_preparation_ready()
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.get("/api/health")
    def health():
        return {"service": "paperloom", "version": "0.3.4", "requires_token": bool(access_token)}

    @app.get("/api/jobs")
    def jobs():
        return manager.snapshot()

    @app.post("/api/models/discover")
    def models_discover(config: LLMConfig):
        try:
            return discover_models(config)
        except LLMError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/models/test")
    def models_test(config: LLMConfig):
        try:
            return test_connection(config)
        except LLMError as exc:
            raise HTTPException(502, str(exc))

    @app.post("/api/jobs", status_code=202)
    async def submit(file: UploadFile = File(...), config: str = Form(...)):
        try:
            settings = LLMConfig.model_validate_json(config)
        except (ValueError, ValidationError):
            raise HTTPException(422, "模型配置无效")
        preparation_config(settings)
        data = bytearray()
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > 80 * 1024 * 1024:
                raise HTTPException(413, "PDF 最大 80 MB")
        await file.close()
        if not bytes(data[:1024]).lstrip().startswith(b"%PDF-"):
            raise HTTPException(415, "文件不是有效 PDF")
        filename = Path((file.filename or "paper.pdf").replace("\\", "/")).name[:180]
        try:
            jid = manager.new(bytes(data), filename, settings)
        except ValueError as exc:
            raise HTTPException(429, str(exc))
        return {"id": jid}

    @app.get("/api/jobs/{job_id}")
    def status(job_id: str):
        directory(job_id)
        return manager.snapshot(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        directory(job_id)
        manager.cancel(job_id)
        return manager.snapshot(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str, config: LLMConfig):
        directory(job_id)
        if manager.snapshot(job_id)["status"] not in ("failed", "cancelled", "interrupted"):
            raise HTTPException(409, "只能重试失败、取消或中断的任务")
        preparation_config(config)
        try:
            manager.start(job_id, config)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"id": job_id}

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id:str):
        directory(job_id)
        try:manager.delete(job_id)
        except ValueError as exc:raise HTTPException(409,str(exc))
        except OSError:raise HTTPException(500,'任务文件删除未完成，请检查文件占用后重试')
        return {'deleted':job_id}

    @app.post('/api/jobs/{job_id}/reprepare',status_code=202)
    def reprepare(job_id: str,config: LLMConfig):
        path=ready(job_id)
        preparation_config(config)
        revision=manager.snapshot(job_id)['revision']
        history=path/'history';history.mkdir(exist_ok=True)
        snapshot=history/f'revision-{revision}.folio'
        if not snapshot.exists():
            import shutil
            shutil.copyfile(path/'paper.folio',snapshot)
        try:
            manager.start(job_id,config)
        except ValueError as exc:
            raise HTTPException(409,str(exc))
        return {'id':job_id,'previous_version':snapshot.name}

    @app.get("/api/jobs/{job_id}/document")
    def document(job_id: str):
        path = ready(job_id)
        return JSONResponse(json.loads((path / "document.json").read_text(encoding="utf-8")))

    @app.get("/api/jobs/{job_id}/files/{file_path:path}")
    def files(job_id: str, file_path: str):
        path = directory(job_id)
        if not safe_path(file_path) or not (file_path.startswith(("assets/", "pages/")) or file_path == "source.pdf"):
            raise HTTPException(404, "文件不存在")
        file = (path / file_path).resolve()
        if not file.is_relative_to(path.resolve()) or not file.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(file, media_type=mimetypes.guess_type(file.name)[0])

    @app.get("/api/jobs/{job_id}/package")
    def download(job_id: str):
        path = ready(job_id)
        name = re.sub(r"[^\w\-. ]", "_", manager.snapshot(job_id)["filename"])[:140]
        return FileResponse(path / "paper.folio", filename=Path(name).stem + ".folio", media_type="application/zip")

    @app.patch("/api/jobs/{job_id}/review")
    @app.post("/api/jobs/{job_id}/crop")
    def retired_manual_review(job_id:str):
        directory(job_id)
        raise HTTPException(410,"人工校对已由独立校验 agent 替代，请刷新页面并重新准备")

    app.mount("/", StaticFiles(directory=ROOT / "web" / "dist", html=True), name="reader")
    return app
