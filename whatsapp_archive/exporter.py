"""Step 1: export an iPhone backup in four resumable steps.

Finished steps are recorded in data/export_state.json. After a stop, a crash or a power cut
the next run continues from the first unfinished step of the same backup.
"""
from __future__ import annotations

import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from Whatsapp_Chat_Exporter.utility import WhatsAppIdentifier, get_file_name

from .config import DATA_DIR
from .jobs import Cancelled, JobManager
from .markdown import export_markdown
from .store import connect

RESULT_JSON = "result.json"
RESULT_HTML = "result"
STATE_FILE = "export_state.json"
STEPS = ("extract", "json", "ingest", "markdown", "html")
STEP_NAMES = {
    "extract": "Шаг 1 из 5: файлы из бэкапа",
    "json": "Шаг 2 из 5: чтение сообщений",
    "ingest": "Шаг 3 из 5: загрузка в архив",
    "markdown": "Шаг 4 из 5: Markdown-файлы чатов",
    "html": "Шаг 5 из 5: страницы чатов",
}
PROGRESS = re.compile(r"\d+%\|[^|]*\|\s*(\d+)/(\d+)")


def backup_roots() -> list[Path]:
    home = Path.home()
    return [
        home / "Apple" / "MobileSync" / "Backup",
        Path(os.environ.get("APPDATA", home)) / "Apple Computer" / "MobileSync" / "Backup",
        Path(os.environ.get("LOCALAPPDATA", home)) / "Apple Computer" / "MobileSync" / "Backup",
        home / "Library" / "Application Support" / "MobileSync" / "Backup",
    ]


def list_backups() -> list[dict]:
    backups = []
    for root in backup_roots():
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            if not (folder / "Manifest.db").exists():
                continue
            info: dict = {}
            encrypted = False
            try:
                info = plistlib.loads((folder / "Info.plist").read_bytes())
                encrypted = bool(plistlib.loads((folder / "Manifest.plist").read_bytes()).get("IsEncrypted"))
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
            date = info.get("Last Backup Date")
            backups.append({
                "path": str(folder),
                "device": info.get("Device Name") or folder.name,
                "ios": info.get("Product Version"),
                "date": date.strftime("%Y-%m-%d %H:%M") if date else None,
                "encrypted": encrypted,
            })
    return sorted(backups, key=lambda item: item["date"] or "", reverse=True)


def load_state() -> dict | None:
    try:
        return json.loads((DATA_DIR / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(state: dict) -> None:
    temporary = DATA_DIR / f"{STATE_FILE}.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, DATA_DIR / STATE_FILE)


def resume_info() -> dict | None:
    """Describes an unfinished export for the UI."""
    state = load_state()
    if not state or set(STEPS) <= set(state["done"]):
        return None
    return {"backup": state["backup"], "date": state.get("date"), "steps_done": len(state["done"]), "steps": len(STEPS)}


def clear_previous_export() -> None:
    """Removes derived files of the previous export. Hard links do not affect the backup."""
    (DATA_DIR / STATE_FILE).unlink(missing_ok=True)
    for name in (WhatsAppIdentifier.DOMAIN, RESULT_HTML):
        target = DATA_DIR / name
        if target.exists():
            shutil.rmtree(target)
    for name in (WhatsAppIdentifier.MESSAGE, WhatsAppIdentifier.CONTACT, WhatsAppIdentifier.CALL, RESULT_JSON):
        (DATA_DIR / name).unlink(missing_ok=True)


def read_progress(path: Path) -> tuple[int, int] | None:
    """Only the tqdm counter is read from the log, never message content."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 400))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    matches = PROGRESS.findall(tail)
    return (int(matches[-1][0]), int(matches[-1][1])) if matches else None


def run_child(job: JobManager, arguments: list[str]) -> None:
    command = [sys.executable, "-m", "whatsapp_archive.child", *arguments]
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    log_path = DATA_DIR / "export.log"
    with open(log_path, "wb") as log_file:
        process = subprocess.Popen(command, cwd=DATA_DIR, env=environment, stdin=subprocess.PIPE,
                                   stdout=log_file, stderr=subprocess.STDOUT)
        while True:
            try:
                code = process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                try:
                    job.check()
                except Cancelled:
                    process.kill()
                    process.wait()
                    raise
                progress = read_progress(log_path)
                if progress:
                    job.progress.done, job.progress.total = progress
    if code != 0:
        raise RuntimeError(f"Шаг завершился с кодом {code}, подробности в data/export.log")


def clean_text(value, safe: bool) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"<br\s*/?>", "\n", value)
    if safe:
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text.strip() or None


def media_durations() -> dict[str, float]:
    """Durations are not in the exporter's JSON, so they are read from the WhatsApp database copy."""
    path = DATA_DIR / WhatsAppIdentifier.MESSAGE
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT ZMEDIALOCALPATH, ZMOVIEDURATION FROM ZWAMEDIAITEM "
                            "WHERE ZMEDIALOCALPATH IS NOT NULL AND ZMOVIEDURATION > 0")
        return {os.path.basename(p): d for p, d in rows}
    finally:
        conn.close()


def page_name(jid: str, name: str | None) -> str | None:
    try:
        return get_file_name(jid, SimpleNamespace(name=name))[0] + ".html"
    except ValueError:
        return None


def ingest(job: JobManager) -> str:
    """Replaces the archive in one transaction, so a stop keeps the previous version intact."""
    with open(DATA_DIR / RESULT_JSON, encoding="utf-8") as handle:
        data = json.load(handle)
    durations = media_durations()
    job.progress.total = len(data)
    total = 0
    with connect() as db:
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM chats")
        for jid, chat in data.items():
            job.check()
            messages = chat.get("messages") or {}
            raw_name = chat.get("name")
            db.execute("INSERT OR REPLACE INTO chats(jid,name,is_group,messages,page) VALUES(?,?,?,?,?)",
                       (jid, raw_name or jid.split("@")[0], int(jid.endswith("@g.us")), len(messages),
                        page_name(jid, raw_name) if messages else None))
            # The exporter stores media paths relative to this folder.
            media_base = chat.get("media_base") or f"{WhatsAppIdentifier.DOMAIN}/"
            rows = []
            for key, message in messages.items():
                is_media = bool(message.get("media"))
                mime = message.get("mime")
                is_contact_card = (mime or "").startswith("text/x-vcard")  # its data is text, not a file
                path = None
                if is_media and mime != "media" and not is_contact_card and isinstance(message.get("data"), str):
                    path = media_base + message["data"].replace("\\", "/")
                text = message.get("caption") if is_media and not is_contact_card else message.get("data")
                rows.append((
                    jid, message.get("key_id") or key, message.get("timestamp") or 0, int(bool(message.get("from_me"))),
                    message.get("sender"), clean_text(text, bool(message.get("safe"))), path, mime,
                    durations.get(os.path.basename(path)) if path else None, int(bool(message.get("meta"))),
                ))
            db.executemany(
                "INSERT INTO messages(chat_jid,key_id,ts,from_me,sender,text,media_path,mime,duration,is_meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            job.progress.done += 1
    return f"Чатов: {len(data)}, сообщений: {total}."


def write_index() -> None:
    """The exporter makes one page per chat, so the list of chats is generated here."""
    with connect() as db:
        rows = db.execute("SELECT name, messages, page FROM chats WHERE page IS NOT NULL ORDER BY messages DESC").fetchall()
    links = "".join(
        f'<li data-name="{html.escape(row["name"].casefold(), quote=True)}">'
        f'<a href="{html.escape(row["page"], quote=True)}">{html.escape(row["name"])}</a> <small>{row["messages"]}</small></li>'
        for row in rows)
    page = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Чаты WhatsApp</title>'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px}'
        'input{width:100%;padding:9px;font:inherit;box-sizing:border-box}ul{list-style:none;padding:0}li{padding:6px 0;border-bottom:1px solid #8884}'
        'small{opacity:.65;margin-left:6px}</style></head><body><h1>Чаты WhatsApp</h1>'
        '<input id="q" type="search" placeholder="Поиск по названию" aria-label="Поиск по названию">'
        f'<ul>{links}</ul><script>const q=document.getElementById("q");q.oninput=()=>{{const v=q.value.toLowerCase();'
        'document.querySelectorAll("li").forEach(r=>{r.hidden=!r.dataset.name.includes(v)})}}</script></body></html>')
    (DATA_DIR / RESULT_HTML / "index.html").write_text(page, encoding="utf-8")


def step_commands(backup: str, link_media: bool) -> dict[str, list[str]]:
    call_db = str(WhatsAppIdentifier.CALL)
    return {
        "extract": ["extract", backup, "1" if link_media else "0"],
        "json": ["export", "-i", "--call-db", call_db, "-j", RESULT_JSON, "--no-html", "--no-banner"],
        "html": ["export", "-i", "--call-db", call_db, "-o", RESULT_HTML, "--resume-html", "--no-banner"],
    }


def run(job: JobManager, backup: str, link_media: bool = True, fresh: bool = False) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date = next((item["date"] for item in list_backups() if item["path"] == backup), None)
    state = load_state()
    same = state and state["backup"] == backup and state.get("date") == date
    if fresh or not same:
        job.stage("Подготовка: удаление прошлой выгрузки")
        clear_previous_export()
        state = {"backup": backup, "date": date, "link_media": link_media, "done": []}
        save_state(state)
    commands = step_commands(backup, state["link_media"])
    summary = None
    for step in STEPS:
        if step in state["done"]:
            continue
        job.stage(STEP_NAMES[step])
        if step == "ingest":
            summary = ingest(job)
        elif step == "markdown":
            note = export_markdown(job)  # runs before the slow HTML step, so the files appear early
            summary = f"{summary} {note}" if summary else note
        else:
            run_child(job, commands[step])
        if step == "html":
            write_index()
        state["done"].append(step)
        save_state(state)
    return summary or "Выгрузка завершена."
