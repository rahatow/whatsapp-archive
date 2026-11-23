"""Local web app. Binds to 127.0.0.1 only and protects the API from other websites in the same browser."""
from __future__ import annotations

import os
import secrets
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from Whatsapp_Chat_Exporter.utility import WhatsAppIdentifier

from . import exporter, markdown, rag, transcribe
from .config import DATA_DIR, api_host, api_key, load_settings, media_file, save_settings
from .jobs import JobManager
from .store import connect

TOKEN = secrets.token_urlsafe(32)
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
jobs = JobManager()


@app.middleware("http")
async def protect(request: Request, call_next):
    # Host check stops DNS rebinding. The token stops requests from other websites.
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    if request.url.path.startswith("/api/") and not secrets.compare_digest(request.headers.get("x-token", ""), TOKEN):
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


class ExportRequest(BaseModel):
    backup: str
    link_media: bool = True
    fresh: bool = False


class TranscribeRequest(BaseModel):
    audio: bool = True
    video: bool = True
    chat: Optional[str] = None
    limit: Optional[int] = None
    retry_failed: bool = False
    then_index: bool = True
    confirm_external: bool = False


class IndexRequest(BaseModel):
    limit: Optional[int] = None
    chat: Optional[str] = None
    confirm_external: bool = False


class AskRequest(BaseModel):
    question: str
    chat: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    k: int = 8
    generate: bool = True
    confirm_external: bool = False


def require_confirmation(external: bool, confirmed: bool) -> None:
    """Private data leaves this computer only after an explicit confirmation in the UI."""
    if external and not confirmed:
        raise HTTPException(status_code=409, detail="Нужно подтвердить отправку данных во внешний сервис")


def start(kind: str, target) -> dict:
    if not jobs.start(kind, target):
        raise HTTPException(status_code=409, detail="Уже выполняется другая задача")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)


@app.get("/api/status")
def status() -> dict:
    settings = load_settings()
    with connect() as db:
        chats, messages = db.execute("SELECT COUNT(*), COALESCE(SUM(messages),0) FROM chats").fetchone()
        done = db.execute("SELECT COUNT(*) FROM transcripts WHERE status='done'").fetchone()[0]
    return {
        "job": jobs.progress.as_dict(),
        "chats": chats,
        "messages": messages,
        "has_html": (DATA_DIR / exporter.RESULT_HTML / "index.html").exists(),
        "export_resume": exporter.resume_info(),
        "free_gb": round(shutil.disk_usage(DATA_DIR.anchor).free / 1e9, 1),
        "media": transcribe.media_stats(),
        "transcripts_done": done,
        "rag": rag.stats(),
        "api_key_found": api_key(settings) is not None,
        "api_host": api_host(settings),
        "local_chat_models": rag.local_chat_models(settings),
        "settings": settings,
    }


@app.get("/api/backups")
def backups() -> list[dict]:
    return exporter.list_backups()


@app.get("/api/chats")
def chats() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT jid, name, messages FROM chats ORDER BY name COLLATE NOCASE")]


@app.post("/api/settings")
def update_settings(changes: dict) -> dict:
    return save_settings(changes)


@app.post("/api/cancel")
def cancel() -> dict:
    jobs.cancel()
    return {"ok": True}


@app.post("/api/shutdown")
def shutdown() -> dict:
    """Exits at once. Finished work is already saved, and a child process stops with the app."""
    jobs.cancel()
    threading.Timer(0.5, os._exit, [0]).start()
    return {"ok": True}


@app.post("/api/export")
def export(request: ExportRequest) -> dict:
    known = {item["path"]: item for item in exporter.list_backups()}
    backup = known.get(request.backup)
    if not backup:
        raise HTTPException(status_code=404, detail="Бэкап не найден")
    if backup["encrypted"]:
        raise HTTPException(status_code=400, detail="Бэкап зашифрован. Отключите шифрование и сделайте новый бэкап.")
    return start("export", lambda job: exporter.run(job, request.backup, request.link_media, request.fresh))


@app.post("/api/transcribe")
def run_transcribe(request: TranscribeRequest) -> dict:
    settings = load_settings()
    external = settings["transcribe_backend"] == "api" or (request.then_index and settings["embed_backend"] == "api")
    require_confirmation(external, request.confirm_external)
    kinds = tuple(kind for kind, chosen in (("audio", request.audio), ("video", request.video)) if chosen)
    if not kinds:
        raise HTTPException(status_code=400, detail="Выберите аудио или видео")

    def work(job: JobManager) -> str | None:
        note = transcribe.run(job, kinds, request.chat, request.limit, request.retry_failed)
        if request.then_index:
            index_note = rag.index(job, None, request.chat)  # adds the new transcripts to search
            return " ".join(part for part in (note, index_note) if part) or None
        return note

    return start("transcribe", work)


@app.post("/api/markdown")
def run_markdown() -> dict:
    if not load_settings()["markdown_dir"]:
        raise HTTPException(status_code=400, detail="Сначала укажите папку для Markdown")
    return start("markdown", lambda job: markdown.export_markdown(job))


@app.post("/api/index")
def run_index(request: IndexRequest) -> dict:
    require_confirmation(load_settings()["embed_backend"] == "api", request.confirm_external)
    return start("index", lambda job: rag.index(job, request.limit, request.chat))


def to_timestamp(value: Optional[str], end_of_day: bool = False) -> Optional[float]:
    if not value:
        return None
    moment = datetime.strptime(value, "%Y-%m-%d")
    return moment.replace(hour=23, minute=59, second=59).timestamp() if end_of_day else moment.timestamp()


@app.post("/api/ask")
def ask(request: AskRequest) -> dict:
    settings = load_settings()
    external = settings["embed_backend"] == "api" or (request.generate and settings["chat_backend"] == "api")
    require_confirmation(external, request.confirm_external)
    try:
        sources = rag.search(request.question, request.k, request.chat,
                             to_timestamp(request.date_from), to_timestamp(request.date_to, True))
        text = rag.answer(request.question, sources) if request.generate and sources else None
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"{type(error).__name__}: {error}"[:400]) from None
    return {"answer": text, "sources": sources}


class ExportedFiles(StaticFiles):
    """Serves only chat pages and media. The database and settings in the same folder stay private."""

    PUBLIC = (exporter.RESULT_HTML, str(WhatsAppIdentifier.DOMAIN))

    async def get_response(self, path, scope):
        parts = Path(path).parts
        if not parts or parts[0] not in self.PUBLIC:
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except Exception as error:  # Starlette raises HTTPException(404) for a missing file
            if getattr(error, "status_code", None) != 404 or parts[0] != str(WhatsAppIdentifier.DOMAIN):
                raise
            alt = media_file(Path(path).as_posix())  # media removed locally: serve the NAS copy
            if alt.is_file():
                return FileResponse(alt)
            raise


DATA_DIR.mkdir(parents=True, exist_ok=True)
# The exporter's pages link to media by paths relative to the data folder.
app.mount("/files", ExportedFiles(directory=DATA_DIR, follow_symlink=False), name="files")
