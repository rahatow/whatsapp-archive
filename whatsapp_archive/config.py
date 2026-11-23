"""Settings and paths. API keys come only from environment variables and are never written to disk."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("WA_ARCHIVE_DATA", Path.cwd() / "data")).resolve()
SETTINGS_PATH = DATA_DIR / "settings.json"

# Optional media fallback: a second copy of the iPhone media tree (…/Message/Media), for example on a NAS.
# When a file is missing under DATA_DIR (e.g. the local copy was removed to free disk space), it is read from
# there by the same relative path. Several roots: separate with ";" in WA_MEDIA_FALLBACK. Empty by default.
MEDIA_FALLBACK_ROOTS = [Path(p) for p in os.environ.get(
    "WA_MEDIA_FALLBACK", "").split(";") if p]


def media_file(rel: str | Path) -> Path:
    """Local file under DATA_DIR if present, else the NAS copy of …/Message/Media/<rest>, else the local path."""
    local = DATA_DIR / rel
    if local.exists():
        return local
    text = str(rel).replace("\\", "/")
    marker = "Message/Media/"
    if marker in text:
        rest = text.split(marker, 1)[1]
        for root in MEDIA_FALLBACK_ROOTS:
            try:
                candidate = root / "Media" / rest
                if candidate.exists():
                    return candidate
            except OSError:  # NAS not reachable
                continue
    return local

DEFAULTS = {
    "transcribe_backend": "local",  # local | api
    "whisper_model": "large-v3-turbo",
    "api_transcribe_model": "gpt-4o-mini-transcribe",
    "api_transcribe_usd_per_min": 0.003,
    "embed_backend": "local",  # local | api
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_embed_model": "bge-m3",
    "ollama_chat_model": "",
    "api_embed_model": "text-embedding-3-small",
    "chat_backend": "local",  # local | api
    "api_chat_model": "gpt-4o-mini",
    "api_base_url": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY",
    "markdown_dir": "",  # when set, every export refreshes one Markdown file per chat there
    "owner_name": "Я",
}


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    return settings


def save_settings(changes: dict) -> dict:
    settings = load_settings()
    settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def api_key(settings: dict) -> str | None:
    return os.environ.get(settings["api_key_env"]) or None


def api_host(settings: dict) -> str:
    return urlparse(settings["api_base_url"]).netloc
