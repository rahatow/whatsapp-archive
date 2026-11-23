"""SQLite archive: messages, transcripts, RAG chunks and embeddings."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY, name TEXT NOT NULL, is_group INTEGER NOT NULL,
    messages INTEGER NOT NULL DEFAULT 0, page TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    chat_jid TEXT NOT NULL REFERENCES chats(jid),
    key_id TEXT,
    ts REAL NOT NULL,
    from_me INTEGER NOT NULL,
    sender TEXT,
    text TEXT,
    media_path TEXT,
    mime TEXT,
    duration REAL,
    is_meta INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_jid, ts);
CREATE INDEX IF NOT EXISTS messages_media ON messages(media_path);
CREATE TABLE IF NOT EXISTS transcripts (
    media_path TEXT PRIMARY KEY,
    status TEXT NOT NULL,            -- done | failed
    text TEXT,
    language TEXT,
    backend TEXT,
    model TEXT,
    error TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS chunks (
    hash TEXT PRIMARY KEY,
    chat_jid TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(hash UNINDEXED, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS embeddings (
    hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (hash, model)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS media_notes (  -- descriptions of photos, stickers, documents, videos (filled by external tools)
    media_path TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
    description TEXT, content TEXT, pages INTEGER, has_audio INTEGER, sha1 TEXT,
    model TEXT, error TEXT, updated_at REAL
);
"""


DB_FOLDER = "база_архива"  # сообщения, расшифровки голосовых и видео, индекс поиска


def db_path() -> Path:
    """data/база_архива/archive.db; старое место data/archive.db используется, пока базу не перенесли."""
    new = DATA_DIR / DB_FOLDER / "archive.db"
    old = DATA_DIR / "archive.db"
    return old if old.exists() and not new.exists() else new


def connect() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
