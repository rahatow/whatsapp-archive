"""Write every chat as a Markdown file into a folder, for example a personal knowledge base.

One file per chat. Files are tracked by chat id in a small manifest, so a renamed contact
renames its file instead of leaving a duplicate. Unchanged files are not rewritten, files
this tool did not create are never touched, and a chat missing from a newer backup keeps
its file.

    python -m whatsapp_archive.markdown --target "C:/path/to/folder" --owner "Your name"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from Whatsapp_Chat_Exporter.utility import WhatsAppIdentifier

from .config import DATA_DIR, load_settings, save_settings
from .jobs import JobManager
from .store import connect

MANIFEST = ".whatsapp-archive.json"
MIN_TS = 86400  # earlier values are broken dates, and Windows cannot convert negative ones
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 120


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", (value or "").split("@")[0].split(":")[0])


def lid_phones() -> dict[str, str]:
    """Phone numbers for chats known only by an internal @lid id, from WhatsApp's LID.sqlite."""
    path = DATA_DIR / str(WhatsAppIdentifier.DOMAIN) / "LID.sqlite"
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {digits(lid): digits(phone) for lid, phone in
                conn.execute("SELECT ZIDENTIFIER, ZPHONENUMBER FROM ZWAZACCOUNT WHERE ZPHONENUMBER IS NOT NULL")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def chat_kind(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "группа"
    if jid.endswith("@broadcast"):
        return "рассылка"
    if "@" not in jid:
        return "журнал звонков"
    return "личный"


def phone_of(jid: str, lids: dict[str, str]) -> str | None:
    if jid.endswith("@s.whatsapp.net"):
        return "+" + digits(jid)
    if jid.endswith("@lid") and lids.get(digits(jid)):
        return "+" + lids[digits(jid)]
    return None  # an unmapped @lid id is not a phone number


def safe_filename(value: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    name = re.sub(r"\s+", " ", name).strip(" .")[:MAX_NAME].strip(" .") or "chat"
    return f"_{name}" if name.split(".")[0].upper() in RESERVED else name


def desired_names(chats: list[dict]) -> dict[str, str]:
    """File names without extension. Names that clash ignoring case get a short id suffix."""
    names = {}
    for chat in chats:
        name, phone = chat["name"], chat["phone"]
        if chat["kind"] == "группа":
            base = f"{name} (группа)"
        elif phone and digits(name) != digits(phone):
            base = f"{name} {phone}"
        elif phone:
            base = phone
        else:
            base = name
        names[chat["jid"]] = safe_filename(base)
    clashes: dict[str, list[str]] = {}
    for jid, name in names.items():
        clashes.setdefault(name.casefold(), []).append(jid)
    for jids in clashes.values():
        if len(jids) > 1:
            for jid in jids:
                suffix = digits(jid)[-6:] or str(abs(hash(jid)) % 10**6)
                names[jid] = safe_filename(f"{names[jid][:MAX_NAME - 8]} {suffix}")
    seen: set[str] = set()
    for jid in sorted(names):  # last resort for clashes that survive the suffix
        if names[jid].casefold() in seen:
            names[jid] = safe_filename(f"{names[jid]} {digits(jid) or 'x'}")
        seen.add(names[jid].casefold())
    return names


def attachment(row) -> str | None:
    if not row["media_path"]:
        return None
    mime = (row["mime"] or "").lower()
    length = ""
    if row["duration"]:
        length = f" {int(row['duration']) // 60}:{int(row['duration']) % 60:02d}"
    if mime.startswith("audio/"):
        return f"*[голосовое{length}]*"
    if mime.startswith("video/"):
        return f"*[видео{length}]*"
    if mime == "image/webp":
        return "*[стикер]*"
    if mime.startswith("image/"):
        return "*[фото]*"
    return f"*[файл: {row['media_path'].rsplit('/', 1)[-1]}]*"


DOC_EXCERPT = 500
SEEN_TEXT = 600
DOCS_SUFFIX = "_документы"  # folder next to a chat file with the full text of its documents


def _field(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def doc_title(row) -> str:
    """The original file name: WhatsApp keeps it as the message text of a document."""
    text = (_field(row, "text") or "").strip()
    if text and "\n" not in text and len(text) < 150 and "." in text[-6:]:
        return text
    return (row["media_path"] or "документ").replace("\\", "/").rsplit("/", 1)[-1]


def has_doc_file(row) -> bool:
    return (_field(row, "note_kind") == "document" and bool(_field(row, "note_desc"))
            and len(_field(row, "note_content") or "") > DOC_EXCERPT)


UUID_LIKE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{20,}$")


def doc_display_title(row) -> str:
    """Original file name, or the first words of the description when WhatsApp kept only an id."""
    title = doc_title(row)
    if UUID_LIKE.match(os.path.splitext(title)[0]) and _field(row, "note_desc"):
        desc = re.sub(r"^(Документ|Это|Данный документ)\s*(—|-|–)?\s*(это)?\s*", "", row["note_desc"].strip(), flags=re.I)
        return " ".join(desc.split()[:7]).rstrip(",.;:")
    return title


def doc_file_name(row) -> str:
    stem = doc_display_title(row)
    if stem == doc_title(row):
        stem = os.path.splitext(stem)[0]
    stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")[:60] or "документ"
    tag = hashlib.sha1(row["media_path"].encode("utf-8")).hexdigest()[:6]
    moment = datetime.fromtimestamp(max(row["ts"], MIN_TS))
    return f"{moment:%Y-%m-%d_%H%M}_{stem}_{tag}.md"


def doc_link(docs_dir: str, row) -> str:
    # only what breaks a Markdown link is escaped, Cyrillic stays readable
    return f"{docs_dir}/{doc_file_name(row)}".replace("%", "%25").replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def write_doc_files(folder: Path, rows, chat_name: str, owner: str) -> tuple[int, int]:
    """One Markdown file per long document with its full text. Returns (written, unchanged)."""
    docs = [row for row in rows if has_doc_file(row)]
    written = unchanged = 0
    keep = set()
    if docs:
        folder.mkdir(parents=True, exist_ok=True)
    for row in docs:
        name = doc_file_name(row)
        keep.add(name)
        moment = datetime.fromtimestamp(max(row["ts"], MIN_TS))
        who = owner if row["from_me"] else (_field(row, "sender") or chat_name)
        pages = _field(row, "note_pages")
        lines = [f"# {doc_display_title(row)}", "",
                 f"- Имя файла: {doc_title(row)}",
                 f"- Чат: {chat_name}",
                 f"- Дата: {moment:%d.%m.%Y %H:%M}, отправил(а): {who}",
                 f"- Файл в архиве: {row['media_path']}" + (f" ({pages} стр.)" if pages else ""),
                 f"- Кратко (авто): {' '.join(row['note_desc'].split())}",
                 "", "Ниже полный текст документа, извлечённый автоматически.", "", "---", "",
                 row["note_content"].strip(), ""]
        if write_if_changed(folder / name, "\n".join(lines)):
            written += 1
        else:
            unchanged += 1
    if folder.is_dir() and folder.name.endswith(DOCS_SUFFIX):
        for old in folder.glob("*.md"):  # this folder is generated: drop files of documents that are gone
            if old.name not in keep:
                old.unlink()
    return written, unchanged


def media_note(row, link: str | None = None) -> str | None:
    """Automatic description of an attachment (table media_notes), when the query selected it."""
    try:
        kind, desc, content = row["note_kind"], row["note_desc"], row["note_content"]
    except (IndexError, KeyError):
        return None
    if not desc:
        return None
    desc = " ".join(desc.split())
    content = " ".join((content or "").split())
    if kind == "sticker":
        return f"*({desc})*"
    if kind == "video":
        return f"**Что на видео (авто):** {desc}"
    if kind == "photo":
        out = f"**На фото (авто):** {desc}"
        if content:
            out += f"  \n**Текст на фото:** {content[:SEEN_TEXT]}{'…' if len(content) > SEEN_TEXT else ''}"
        return out
    out = f"**Документ (авто):** {desc}"
    if content:
        out += f"  \n**Начало текста:** {content[:DOC_EXCERPT]}{'…' if len(content) > DOC_EXCERPT else ''}"
    if link:
        out += f"  \n[Полный текст документа]({link})"
    return out


def message_text(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    lines = []
    for line in text.strip().splitlines():
        # A line starting with # or > would turn into a heading or a quote in Markdown.
        lines.append("\\" + line if line.lstrip().startswith(("#", ">")) else line)
    return "  \n".join(lines)


def render(chat: dict, rows: list, owner: str, source: str, transcripts: bool) -> tuple[str, int]:
    body: list[str] = []
    count = 0
    day = None
    dated = [row for row in rows if row["ts"] >= MIN_TS]
    undated = [row for row in rows if row["ts"] < MIN_TS]
    first = last = None
    for group, rows_in_group in (("dated", dated), ("undated", undated)):
        for row in rows_in_group:
            parts = [attachment(row)]
            if transcripts:
                docs_dir = chat.get("docs_dir")
                parts.append(media_note(row, doc_link(docs_dir, row) if docs_dir and has_doc_file(row) else None))
            if transcripts and row["transcript"]:
                parts.append(row["transcript"].strip())
            text = message_text(row["text"])
            if text and not (row["media_path"] and row["text"].strip() == row["media_path"].rsplit("/", 1)[-1]):
                parts.append(text)
            parts = [part for part in parts if part]
            if not parts:
                continue
            if group == "dated":
                moment = datetime.fromtimestamp(row["ts"])
                first, last = first or moment, moment
                if moment.date() != day:
                    day = moment.date()
                    body += ["", f"## {moment:%d.%m.%Y}, {WEEKDAYS[moment.weekday()]}", ""]
                stamp = f"{moment:%H:%M}"
            else:
                if day != "undated":
                    day = "undated"
                    body += ["", "## Дата неизвестна", ""]
                stamp = "??:??"
            if row["from_me"]:
                who = owner
            elif chat["kind"] == "группа":
                who = row["sender"] or "Неизвестный"
            else:
                who = chat["name"]
            body.append(f"**{stamp} {who}:** {' '.join(parts)}  ")
            count += 1
    meta = {
        "name": chat["name"],
        "phone": chat["phone"],
        "whatsapp_id": chat["jid"],
        "type": chat["kind"],
        "messages": count,
        "first_message": f"{first:%Y-%m-%d}" if first else None,
        "last_message": f"{last:%Y-%m-%d}" if last else None,
        "source": source,
    }
    front = ["---"] + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in meta.items()
                       if value is not None] + ["---", "", f"# {chat['name']}"]
    return "\n".join(front + body).rstrip() + "\n", count


def write_if_changed(path: Path, content: str) -> bool:
    data = content.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False  # unchanged files keep their date, so sync tools see no change
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return True


def load_manifest(target: Path) -> dict[str, str]:
    try:
        return json.loads((target / MANIFEST).read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError):
        return {}


def backup_label() -> str:
    try:
        state = json.loads((DATA_DIR / "export_state.json").read_text(encoding="utf-8"))
        return f"WhatsApp, бэкап iPhone от {state.get('date') or 'неизвестной даты'}"
    except (OSError, ValueError):
        return "WhatsApp, бэкап iPhone"


def export_markdown(job: JobManager | None = None, target: str | None = None, owner: str | None = None,
                    transcripts: bool = False) -> str:
    settings = load_settings()
    target = target or settings["markdown_dir"]
    owner = owner or settings["owner_name"]
    if not target:
        return "Папка для Markdown не задана, файлы не обновлялись."
    folder = Path(target)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return f"Папка для Markdown недоступна ({type(error).__name__}), файлы не обновлялись."

    lids = lid_phones()
    with connect() as db:
        chats = [{"jid": row["jid"], "name": row["name"], "kind": chat_kind(row["jid"]),
                  "phone": phone_of(row["jid"], lids)}
                 for row in db.execute("SELECT jid, name FROM chats ORDER BY jid")]
        names = desired_names(chats)
        manifest = load_manifest(folder)
        source = backup_label()
        if job:
            job.progress.total = len(chats)
        written = unchanged = renamed = 0
        transcript_sql = ("LEFT JOIN transcripts t ON t.media_path=m.media_path AND t.status='done'" if transcripts
                          else "LEFT JOIN (SELECT NULL media_path, NULL text) t ON 0")
        for chat in chats:
            if job:
                job.check()
            rows = db.execute(
                f"""SELECT m.ts, m.from_me, m.sender, m.text, m.media_path, m.mime, m.duration, t.text transcript
                    FROM messages m {transcript_sql}
                    WHERE m.chat_jid=? AND m.is_meta=0 ORDER BY m.ts, m.id""", (chat["jid"],)).fetchall()
            content, count = render(chat, rows, owner, source, transcripts)
            if job:
                job.progress.done += 1
            if not count:
                continue
            filename = names[chat["jid"]] + ".md"
            old = manifest.get(chat["jid"])
            if old and old != filename and (folder / old).exists():
                (folder / old).unlink()  # the contact was renamed, the file follows
                renamed += 1
            if write_if_changed(folder / filename, content):
                written += 1
            else:
                unchanged += 1
            manifest[chat["jid"]] = filename
    write_if_changed(folder / MANIFEST, json.dumps({"version": 1, "files": manifest}, ensure_ascii=False, indent=1))
    return f"Markdown: обновлено файлов {written}, без изменений {unchanged}, переименовано {renamed}."


def main() -> None:
    parser = argparse.ArgumentParser(description="Write every WhatsApp chat as a Markdown file")
    parser.add_argument("--target", help="folder for the files; saved for automatic updates")
    parser.add_argument("--owner", help="your name in the files; saved for automatic updates")
    args = parser.parse_args()
    changes = {key: value for key, value in (("markdown_dir", args.target), ("owner_name", args.owner)) if value}
    if changes:
        save_settings(changes)
    print(export_markdown())


if __name__ == "__main__":
    main()
