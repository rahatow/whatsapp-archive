"""Transcribe voice notes and videos, locally with faster-whisper or through an API. Safe to resume."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import DATA_DIR, api_key, load_settings, media_file
from .jobs import Cancelled, JobManager
from .store import connect

log = logging.getLogger("whatsapp_archive")
API_LIMIT_BYTES = 25 * 1024 * 1024
API_WORKERS = 4


def pending_query(kinds: tuple[str, ...], chat: str | None) -> tuple[str, list]:
    mime = " OR ".join("m.mime LIKE ?" for _ in kinds)
    params: list = [f"{kind}/%" for kind in kinds]
    where = f"m.media_path IS NOT NULL AND ({mime})"
    if chat:
        where += " AND m.chat_jid = ?"
        params.append(chat)
    return where, params


def media_stats(kinds: tuple[str, ...] = ("audio", "video"), chat: str | None = None) -> dict:
    where, params = pending_query(kinds, chat)
    with connect() as db:
        row = db.execute(
            f"""SELECT COUNT(*) total, COALESCE(SUM(pending),0) pending, COALESCE(SUM(minutes),0) pending_minutes, COALESCE(SUM(failed),0) failed
                FROM (SELECT m.media_path,
                             MAX(t.status IS NULL) pending,
                             MAX(t.status='failed') failed,
                             MAX(CASE WHEN t.status IS NULL THEN COALESCE(m.duration,0)/60.0 ELSE 0 END) minutes
                      FROM messages m LEFT JOIN transcripts t ON t.media_path=m.media_path
                      WHERE {where} GROUP BY m.media_path)""", params).fetchone()
    return dict(row)


def pending_files(kinds: tuple[str, ...], chat: str | None, limit: int | None, retry_failed: bool) -> list[str]:
    where, params = pending_query(kinds, chat)
    status = "t.status IS NULL OR t.status='failed'" if retry_failed else "t.status IS NULL"
    sql = (f"SELECT m.media_path, MAX(m.ts) newest FROM messages m LEFT JOIN transcripts t ON t.media_path=m.media_path "
           f"WHERE {where} AND ({status}) GROUP BY m.media_path ORDER BY newest DESC")
    if limit:
        sql += " LIMIT ?"
        params = [*params, int(limit)]
    with connect() as db:
        return [r["media_path"] for r in db.execute(sql, params)]


def save(path: str, status: str, text: str | None, language: str | None, backend: str, model: str, error: str | None) -> None:
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO transcripts(media_path,status,text,language,backend,model,error,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (path, status, text, language, backend, model, error, time.time()))


def has_audio(path: Path) -> bool:
    """False for media without an audio track (faster-whisper fails on those with IndexError)."""
    import av

    with av.open(str(path)) as container:
        return bool(container.streams.audio)


class LocalWhisper:
    """Uses the GPU when it works and falls back to the CPU."""

    BATCH_SIZE = 8  # about 2.5 times faster than one segment at a time on an 8 GB card

    def __init__(self, model_name: str, check=None) -> None:
        from faster_whisper import BatchedInferencePipeline, WhisperModel

        self.factory = WhisperModel
        self.batched = BatchedInferencePipeline
        self.model_name = model_name
        self.check = check or (lambda: None)
        self.note = ""
        try:
            self._set_model(self._load("cuda", "int8_float16"), "cuda")
        except Exception as error:
            self._use_cpu(error)

    def _load(self, device: str, compute_type: str):
        return self.factory(self.model_name, device=device, compute_type=compute_type)

    def _set_model(self, model, device: str) -> None:
        self.model = model
        self.pipeline = self.batched(model=model)
        self.device = device

    def _use_cpu(self, error: Exception) -> None:
        log.warning("GPU is unavailable (%s), using CPU", type(error).__name__)
        self._set_model(self._load("cpu", "int8"), "cpu")
        self.note = "Видеокарта недоступна, расшифровка идёт на процессоре и будет заметно медленнее."

    def transcribe(self, path: Path) -> tuple[str, str | None]:
        if not has_audio(path):
            return "", None  # a silent video: nothing to transcribe, like the API mode
        try:
            return self._run(path, batched=True)
        except RuntimeError as error:
            message = str(error).lower()
            if "out of memory" in message:
                return self._run(path, batched=False)  # a very long file: slower but smaller in memory
            # A missing CUDA library shows up only on the first real run.
            if self.device == "cuda" and ("cublas" in message or "cuda" in message):
                self._use_cpu(error)
                return self._run(path, batched=True)
            raise

    def _run(self, path: Path, batched: bool) -> tuple[str, str | None]:
        if batched:
            segments, info = self.pipeline.transcribe(str(path), batch_size=self.BATCH_SIZE,
                                                      condition_on_previous_text=False)
        else:
            segments, info = self.model.transcribe(str(path), vad_filter=True, condition_on_previous_text=False)
        parts = []
        for segment in segments:  # checked often, so a stop never waits for a long video to finish
            self.check()
            parts.append(segment.text.strip())
        return " ".join(parts).strip(), info.language


def to_small_audio(source: Path, folder: str) -> Path | None:
    """Converts media to small mono MP3 for upload. Returns None when there is no audio track."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Для режима API нужен ffmpeg")
    target = Path(folder) / "audio.mp3"
    result = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "32k", str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        if "does not contain any stream" in result.stderr or "Output file is empty" in result.stderr or target.exists():
            return None
        raise RuntimeError("ffmpeg не смог прочитать файл")
    return target


def transcribe_api(client: httpx.Client, settings: dict, source: Path) -> tuple[str, str | None]:
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as folder:
        audio = to_small_audio(source, folder)
        if audio is None:
            return "", None
        if audio.stat().st_size > API_LIMIT_BYTES:
            raise RuntimeError("Файл слишком длинный для API")
        for attempt in range(5):
            with open(audio, "rb") as handle:
                response = client.post(
                    "/audio/transcriptions",
                    files={"file": ("audio.mp3", handle, "audio/mpeg")},
                    data={"model": settings["api_transcribe_model"]})
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt * 2)
                continue
            response.raise_for_status()
            return response.json().get("text", "").strip(), None
    raise RuntimeError("API не ответил")


def api_client(settings: dict) -> httpx.Client:
    key = api_key(settings)
    if not key:
        raise RuntimeError(f"Не задана переменная окружения {settings['api_key_env']}")
    return httpx.Client(base_url=settings["api_base_url"], headers={"Authorization": f"Bearer {key}"}, timeout=600)


def run(job: JobManager, kinds: tuple[str, ...], chat: str | None, limit: int | None, retry_failed: bool) -> str | None:
    settings = load_settings()
    backend = settings["transcribe_backend"]
    files = pending_files(kinds, chat, limit, retry_failed)
    job.stage("Подготовка", len(files))
    if not files:
        return "Новых файлов для расшифровки нет."
    if backend == "local":
        return _run_local(job, settings, files)
    return _run_api(job, settings, files)


def _handle(job: JobManager, path: str, backend: str, model: str, work) -> None:
    source = media_file(path)  # local copy, else the NAS copy (config.MEDIA_FALLBACK_ROOTS)
    try:
        if not source.exists():
            raise FileNotFoundError("Файла нет в выгрузке")
        text, language = work(source)
        save(path, "done", text, language, backend, model, None)
        job.progress.done += 1
    except Cancelled:
        raise  # the file stays pending and is processed again next time
    except Exception as error:  # one broken file must not stop thousands of others
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (401, 403):
            raise RuntimeError("API отклонил ключ доступа") from None
        log.warning("Transcription failed: %s", type(error).__name__)
        save(path, "failed", None, None, backend, model, f"{type(error).__name__}: {error}"[:300])
        job.progress.failed += 1


def free_ollama_memory(settings: dict) -> None:
    """Ollama keeps models in video memory for minutes. Whisper then crawls, so they are unloaded first."""
    try:
        running = httpx.get(f"{settings['ollama_url']}/api/ps", timeout=5).json().get("models", [])
        for model in running:
            httpx.post(f"{settings['ollama_url']}/api/generate",
                       json={"model": model["name"], "keep_alive": 0}, timeout=30)
    except (httpx.HTTPError, ValueError):
        pass  # Ollama is optional for transcription


def _run_local(job: JobManager, settings: dict, files: list[str]) -> str | None:
    model = settings["whisper_model"]
    job.stage("Загрузка модели", len(files))
    free_ollama_memory(settings)
    whisper = LocalWhisper(model, job.check)
    job.stage("Расшифровка локально", len(files))
    for path in files:
        job.check()
        _handle(job, path, "local", model, whisper.transcribe)
        if whisper.note:
            job.progress.note = whisper.note
    return whisper.note or None


def _run_api(job: JobManager, settings: dict, files: list[str]) -> str | None:
    model = settings["api_transcribe_model"]
    job.stage("Расшифровка через API", len(files))
    with api_client(settings) as client, ThreadPoolExecutor(API_WORKERS) as pool:
        def one(path: str) -> None:
            job.check()
            _handle(job, path, "api", model, lambda source: transcribe_api(client, settings, source))

        for future in [pool.submit(one, path) for path in files]:
            future.result()
    return None
