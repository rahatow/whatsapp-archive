#!/usr/bin/env python3
"""Экспорт переписок WhatsApp из незашифрованного локального бэкапа iPhone.

Бэкап только читается. На выходе: index.html, а также html/, txt/, jsonl/ и media/.
Нужен только Python 3.10+, сторонних библиотек нет.

    python export_whatsapp.py
    python export_whatsapp.py --chat "Имя" --media none
"""
from __future__ import annotations

import argparse
import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

APPLE_EPOCH = 978307200  # 2001-01-01 в секундах Unix
DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
KINDS = {0: "личный", 1: "группа", 2: "рассылка", 4: "сообщество"}
TYPES = {
    1: "фото", 2: "видео", 3: "голосовое", 4: "контакт", 5: "геопозиция", 7: "ссылка",
    8: "документ", 11: "GIF", 12: "возможно, звонок", 14: "удалено или изменено", 15: "стикер",
    38: "одноразовое фото", 39: "одноразовое видео", 46: "опрос", 54: "видео", 59: "звонок", 66: "альбом",
}
SYSTEM_TYPES = {6, 10, 75}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".opus", ".m4a", ".mp3", ".aac", ".ogg", ".wav"}

CSS = """
:root{--bg:#efeae2;--fg:#111b21;--me:#d9fdd3;--them:#fff;--mute:#667781;--line:#d1d7db;--accent:#027eb5}
@media(prefers-color-scheme:dark){:root{--bg:#0b141a;--fg:#e9edef;--me:#005c4b;--them:#202c33;--mute:#8696a0;--line:#2a3942;--accent:#53bdeb}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,"Segoe UI",sans-serif}
header{position:sticky;top:0;background:var(--them);border-bottom:1px solid var(--line);padding:10px 16px;z-index:1}
header a{color:var(--accent);margin-right:12px}
main{max-width:860px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:17px;margin:0;display:inline}
.day{text-align:center;color:var(--mute);font-size:13px;margin:16px 0 8px}
.msg{display:flex;margin:3px 0}
.msg.me{justify-content:flex-end}
.bubble{background:var(--them);border-radius:8px;padding:6px 9px;max-width:min(78%,620px);overflow-wrap:anywhere;white-space:pre-wrap}
.me .bubble{background:var(--me)}
.who{font-size:13px;font-weight:600;color:var(--accent)}
.meta{color:var(--mute);font-size:11px;text-align:right}
.tag{color:var(--mute);font-style:italic}
.sys{text-align:center;color:var(--mute);font-size:13px;margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.quote{display:block;border-left:3px solid var(--accent);padding:2px 8px;margin-bottom:4px;font-size:13px;color:var(--mute);text-decoration:none}
img,video{display:block;max-width:100%;max-height:420px;border-radius:6px}
audio{display:block;max-width:100%}
a{color:var(--accent)}
:target .bubble{outline:2px solid var(--accent)}
input{width:100%;padding:9px 11px;font:inherit;margin-bottom:10px;border:1px solid var(--line);border-radius:6px;background:var(--them);color:var(--fg)}
table{width:100%;border-collapse:collapse}
td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left}
td.n,th.n{text-align:right;white-space:nowrap}
td.d{white-space:nowrap;color:var(--mute)}
@media(max-width:560px){.hide{display:none}.bubble{max-width:90%}}
"""


def fail(message: str) -> None:
    sys.exit(f"Ошибка: {message}")


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    conn.row_factory = sqlite3.Row
    return conn


def find_backup(given: str | None) -> Path:
    if given:
        path = Path(given)
        if not (path / "Manifest.db").exists():
            fail(f"в папке нет Manifest.db: {path}")
        return path
    home = Path.home()
    roots = [
        home / "Apple" / "MobileSync" / "Backup",
        Path(os.environ.get("APPDATA", home)) / "Apple Computer" / "MobileSync" / "Backup",
        Path(os.environ.get("LOCALAPPDATA", home)) / "Apple Computer" / "MobileSync" / "Backup",
        home / "Library" / "Application Support" / "MobileSync" / "Backup",
    ]
    found = [d for root in roots if root.is_dir() for d in root.iterdir() if (d / "Manifest.db").exists()]
    if not found:
        fail("бэкап iPhone не найден, укажите папку через --backup")
    return max(found, key=lambda d: (d / "Manifest.db").stat().st_mtime)


def digits(jid: str | None) -> str:
    return re.sub(r"\D", "", (jid or "").split("@")[0].split(":")[0])


def is_phone_like(value: str) -> bool:
    return not re.sub(r"[\s+\-()\d‑•‎‏‪-‮⁦-⁩]", "", value)


def is_blob(value: str) -> bool:
    """Отсеивает base64 со служебными данными WhatsApp, который лежит в полях имён."""
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        return False
    return (value.startswith("+") and not is_phone_like(value)) or len(value) >= 20


class Names:
    """Имена в порядке приоритета: телефонная книга, имя из чата, имя профиля, номер."""

    def __init__(self, chat_db: sqlite3.Connection, contacts: sqlite3.Connection | None, lid: sqlite3.Connection | None):
        self.book_phone: dict[str, str] = {}
        self.book_lid: dict[str, str] = {}
        self.lid_phone: dict[str, str] = {}
        self.lid_display: dict[str, str] = {}
        self.push: dict[str, str] = {}
        if contacts:
            for r in contacts.execute("SELECT ZWHATSAPPID,ZLID,ZFULLNAME,ZBUSINESSNAME FROM ZWAADDRESSBOOKCONTACT"):
                name = r["ZFULLNAME"] or r["ZBUSINESSNAME"]
                if name and r["ZWHATSAPPID"]:
                    self.book_phone[digits(r["ZWHATSAPPID"])] = name
                if name and r["ZLID"]:
                    self.book_lid[digits(r["ZLID"])] = name
        if lid:
            for r in lid.execute("SELECT ZIDENTIFIER,ZPHONENUMBER,ZDISPLAYNAME FROM ZWAZACCOUNT"):
                key = digits(r["ZIDENTIFIER"])
                if r["ZPHONENUMBER"]:
                    self.lid_phone[key] = digits(r["ZPHONENUMBER"])
                if r["ZDISPLAYNAME"]:
                    self.lid_display[key] = r["ZDISPLAYNAME"]
        for r in chat_db.execute("SELECT ZJID,ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
            if r["ZJID"] and r["ZPUSHNAME"]:
                self.push[digits(r["ZJID"])] = r["ZPUSHNAME"]

    def resolve(self, jid: str | None, *hints: str | None) -> str:
        hints = tuple(h for h in hints if h and not is_blob(h))
        key = digits(jid)
        is_lid = (jid or "").endswith("@lid")
        phone = self.lid_phone.get(key, "") if is_lid else key
        book = (self.book_lid.get(key) if is_lid else None) or self.book_phone.get(phone)
        if book:
            return book
        for hint in hints:
            if not is_phone_like(hint):
                return hint
        named =self.push.get(key) or self.push.get(phone) or self.lid_display.get(key)
        if named:
            return named
        if phone:
            return f"+{phone}"
        if hints:
            return hints[0]
        return f"ID {key}" if key else "Неизвестный"


def reply_id(blob: bytes | None) -> str | None:
    """Достаёт из protobuf ZMETADATA поле 5: stanza id сообщения, на которое ответили."""
    if not blob:
        return None

    def varint(pos: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = blob[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value, pos
            shift += 7

    pos = 0
    try:
        while pos < len(blob):
            key, pos = varint(pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                _, pos = varint(pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            elif wire == 2:
                size, pos = varint(pos)
                if field == 5:
                    return blob[pos:pos + size].decode()
                pos += size
            else:
                return None
    except (IndexError, UnicodeDecodeError):
        return None
    return None


def slugify(title: str, pk: int) -> str:
    clean = re.sub(r'[<>:"/\\|?*#%&\x00-\x1f]', "_", title)
    clean = re.sub(r"\s+", " ", clean).strip(" .")[:60].strip(" .") or "chat"
    return f"{clean}_{pk}"  # суффикс делает имя уникальным и обходит CON, NUL и другие имена Windows


def timestamp(raw: float | None) -> datetime | None:
    if raw is None or raw + APPLE_EPOCH < 86400:
        return None
    try:
        return datetime.fromtimestamp(raw + APPLE_EPOCH).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def linkify(text: str) -> str:
    escaped = html.escape(text)
    return re.sub(r"https?://[^\s<]+", lambda m: f'<a href="{m[0]}" target="_blank" rel="noopener noreferrer">{m[0]}</a>', escaped)


def media_html(rel: str) -> str:
    url = html.escape(quote(rel, safe="/"), quote=True)
    ext = os.path.splitext(rel)[1].lower()
    if ext in IMAGE_EXT:
        return f'<a href="../{url}" target="_blank"><img src="../{url}" loading="lazy" alt=""></a>'
    if ext in VIDEO_EXT:
        return f'<video src="../{url}" controls preload="none"></video>'
    if ext in AUDIO_EXT:
        return f'<audio src="../{url}" controls preload="none"></audio>'
    return f'<a href="../{url}" target="_blank">{html.escape(os.path.basename(rel))}</a>'


def duration_text(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    return f" {int(seconds) // 60}:{int(seconds) % 60:02d}"


class Exporter:
    def __init__(self, backup: Path, out: Path, media_mode: str):
        self.backup, self.out, self.media_mode = backup, out, media_mode
        info = plistlib.loads((backup / "Manifest.plist").read_bytes())
        if info.get("IsEncrypted"):
            fail("бэкап зашифрован. Снимите галочку «Шифровать локальный бэкап» в Apple Devices и сделайте новый бэкап.")
        self.manifest = open_ro(backup / "Manifest.db")
        self.files = {
            r["relativePath"]: r["fileID"]
            for r in self.manifest.execute("SELECT fileID,relativePath FROM Files WHERE domain=? AND flags=1", (DOMAIN,))
        }
        if "ChatStorage.sqlite" not in self.files:
            fail("в бэкапе нет базы WhatsApp (ChatStorage.sqlite)")
        self.db = open_ro(self.backup_file(self.files["ChatStorage.sqlite"]))
        self.names = Names(self.db, self.optional_db("ContactsV2.sqlite"), self.optional_db("LID.sqlite"))
        self.stats = {"chats": 0, "messages": 0, "media_exported": 0, "media_not_in_backup": 0, "media_failed": 0}
        self.link_fell_back = False
        for sub in ("html", "txt", "jsonl"):
            (out / sub).mkdir(parents=True, exist_ok=True)

    def backup_file(self, file_id: str) -> Path:
        return self.backup / file_id[:2] / file_id

    def optional_db(self, rel: str) -> sqlite3.Connection | None:
        return open_ro(self.backup_file(self.files[rel])) if rel in self.files else None

    def export_media(self, local_path: str, slug: str) -> str | None:
        """Возвращает путь файла относительно папки экспорта или None, если файла нет в бэкапе."""
        file_id = self.files.get(local_path) or self.files.get(f"Message/{local_path}")
        source = self.backup_file(file_id) if file_id else None
        if not source or not source.exists():
            self.stats["media_not_in_backup"] += 1
            return None
        name = f"{file_id[:8]}_{os.path.basename(local_path)}"
        rel = f"media/{slug}/{name}"
        if self.media_mode == "none":
            return None
        target = self.out / rel
        try:
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.media_mode == "link":
                    try:
                        os.link(source, target)
                    except OSError:
                        self.link_fell_back = True
                        shutil.copy2(source, target)
                else:
                    shutil.copy2(source, target)
        except OSError:
            self.stats["media_failed"] += 1
            return None
        self.stats["media_exported"] += 1
        return rel

    def chat_sessions(self, needle: str | None) -> list[dict]:
        rows = self.db.execute(
            """SELECT s.Z_PK pk, s.ZSESSIONTYPE kind, s.ZCONTACTJID jid, s.ZPARTNERNAME partner, COUNT(m.Z_PK) n,
                      MAX(m.ZMESSAGEDATE) last
               FROM ZWACHATSESSION s JOIN ZWAMESSAGE m ON m.ZCHATSESSION=s.Z_PK GROUP BY s.Z_PK"""
        ).fetchall()
        chats = []
        for r in rows:
            jid = r["jid"] or ""
            is_group = jid.endswith("@g.us") or r["kind"] in (1, 2, 4)
            title = (r["partner"] or jid) if is_group else self.names.resolve(jid, r["partner"])
            if needle and needle.casefold() not in title.casefold() and needle not in jid:
                continue
            chats.append({"pk": r["pk"], "kind": KINDS.get(r["kind"], f"тип {r['kind']}"), "jid": jid, "title": title,
                          "n": r["n"], "last": r["last"], "group": is_group, "slug": slugify(title, r["pk"])})
        return sorted(chats, key=lambda c: c["last"] or 0, reverse=True)

    def build_message(self, r: sqlite3.Row, chat: dict) -> dict:
        when = timestamp(r["ZMESSAGEDATE"])
        code = r["ZMESSAGETYPE"]
        from_me = bool(r["ZISFROMME"])
        if from_me:
            sender = "Я"
        elif chat["group"]:
            jid = r["ZMEMBERJID"] or r["ZFROMJID"]
            # ZWAMESSAGE.ZPUSHNAME не используем: там base64 со служебными данными, а не имя
            sender = self.names.resolve(jid, r["ZCONTACTNAME"], r["ZFIRSTNAME"])
        else:
            sender = chat["title"]
        text = r["ZTEXT"] or ""
        title = "" if code == 14 else (r["ZTITLE"] or "")
        caption = "" if title == text else title
        if code == 4:
            text = r["ZVCARDNAME"] or text
        location = None
        if code == 5 and r["ZLATITUDE"] is not None and r["ZLONGITUDE"] is not None:
            location = (r["ZLATITUDE"], r["ZLONGITUDE"])
        media = None
        missing = False
        if r["ZMEDIALOCALPATH"]:
            media = self.export_media(r["ZMEDIALOCALPATH"], chat["slug"])
            missing = media is None and self.media_mode != "none"
        elif code in TYPES and code not in (4, 5, 7, 12, 14, 46, 59, 66):
            missing = True
            self.stats["media_not_in_backup"] += 1
        return {
            "id": r["Z_PK"], "stanza_id": r["ZSTANZAID"], "time": when.isoformat(timespec="seconds") if when else None,
            "from_me": from_me, "sender": sender, "type_code": code,
            "type": "system" if code in SYSTEM_TYPES else ("text" if code == 0 else TYPES.get(code, f"тип {code}")),
            "text": text, "caption": caption, "media": media, "media_missing": missing, "location": location,
            "duration": r["ZMOVIEDURATION"] if code in (2, 3, 11, 39, 54) else None,
            "reply_to": reply_id(r["ZMETADATA"]), "starred": bool(r["ZSTARRED"]),
        }

    def tag(self, m: dict) -> str:
        if m["type"] in ("text", "system"):
            return ""
        return f"[{m['type']}{duration_text(m['duration'])}{', файла нет в бэкапе' if m['media_missing'] else ''}]"

    def export_chat(self, chat: dict) -> None:
        slug = chat["slug"]
        html_path = self.out / "html" / f"{slug}.html"
        with open(html_path, "w", encoding="utf-8") as h, \
                open(self.out / "txt" / f"{slug}.txt", "w", encoding="utf-8") as t, \
                open(self.out / "jsonl" / f"{slug}.jsonl", "w", encoding="utf-8") as j:
            title = html.escape(chat["title"])
            h.write(f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                    f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>'
                    f'<link rel="stylesheet" href="../style.css"></head><body><header><a href="../index.html">← Все чаты</a>'
                    f'<h1>{title}</h1></header><main>')
            t.write(f"{chat['title']} ({chat['kind']}, {chat['jid']})\n\n")
            seen: dict[str, tuple[int, str, str]] = {}
            day = None
            rows = self.db.execute(
                """SELECT m.Z_PK,m.ZMESSAGEDATE,m.ZISFROMME,m.ZMESSAGETYPE,m.ZTEXT,m.ZSTANZAID,m.ZSTARRED,m.ZFROMJID,
                          g.ZMEMBERJID,g.ZCONTACTNAME,g.ZFIRSTNAME,i.ZMEDIALOCALPATH,i.ZTITLE,i.ZLATITUDE,i.ZLONGITUDE,
                          i.ZVCARDNAME,i.ZMETADATA,i.ZMOVIEDURATION
                   FROM ZWAMESSAGE m LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK=m.ZGROUPMEMBER
                   LEFT JOIN ZWAMEDIAITEM i ON i.Z_PK=m.ZMEDIAITEM
                   WHERE m.ZCHATSESSION=? ORDER BY m.ZMESSAGEDATE,m.Z_PK""", (chat["pk"],))
            for r in rows:
                m = self.build_message(r, chat)
                self.stats["messages"] += 1
                j.write(json.dumps(m, ensure_ascii=False) + "\n")
                body = "\n".join(x for x in (m["text"], m["caption"]) if x)
                if m["type"] == "system" and not body:
                    continue
                stamp = (m["time"] or "дата неизвестна")[:19].replace("T", " ")
                tag = self.tag(m)
                location = f"{m['location'][0]}, {m['location'][1]}" if m["location"] else ""
                line = " ".join(x for x in (tag, body, location, m["media"] or "") if x)
                if m["type"] == "system":
                    t.write(f"[{stamp}] --- {line}\n")
                else:
                    t.write(f"[{stamp}] {m['sender']}: {line}\n")
                new_day = stamp[:10] if m["time"] else "дата неизвестна"
                if new_day != day:
                    day = new_day
                    h.write(f'<div class="day">{day}</div>')
                if m["type"] == "system":
                    h.write(f'<div class="sys">{html.escape(body)} · {stamp[11:16]}</div>')
                    continue
                self.write_bubble(h, m, chat, body, tag, stamp, seen)
                if m["stanza_id"]:
                    snippet = (body or tag)[:120]
                    seen[m["stanza_id"]] = (m["id"], m["sender"], snippet)
            h.write("</main></body></html>")
        self.stats["chats"] += 1

    def write_bubble(self, h, m: dict, chat: dict, body: str, tag: str, stamp: str, seen: dict) -> None:
        parts = []
        if chat["group"] and not m["from_me"]:
            parts.append(f'<div class="who">{html.escape(m["sender"])}</div>')
        quoted = seen.get(m["reply_to"]) if m["reply_to"] else None
        if quoted:
            parts.append(f'<a class="quote" href="#m{quoted[0]}"><b>{html.escape(quoted[1])}</b><br>{html.escape(quoted[2])}</a>')
        if m["media"]:
            parts.append(media_html(m["media"]))
        if tag and not m["media"]:
            parts.append(f'<div class="tag">{html.escape(tag)}</div>')
        if m["location"]:
            lat, lon = m["location"]
            url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}"
            parts.append(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{lat}, {lon}</a>')
        if body:
            parts.append(f"<div>{linkify(body)}</div>")
        star = "★ " if m["starred"] else ""
        parts.append(f'<div class="meta">{star}{stamp[11:16]}</div>')
        side = "msg me" if m["from_me"] else "msg"
        h.write(f'<div class="{side}" id="m{m["id"]}"><div class="bubble">{"".join(parts)}</div></div>')

    def write_index(self, chats: list[dict]) -> None:
        (self.out / "style.css").write_text(CSS, encoding="utf-8")
        rows = []
        for c in chats:
            when = timestamp(c["last"])
            url = html.escape(quote(f"html/{c['slug']}.html", safe="/"), quote=True)
            rows.append(f'<tr data-s="{html.escape(c["title"].casefold(), quote=True)}"><td><a href="{url}">{html.escape(c["title"])}</a></td>'
                        f'<td class="hide">{c["kind"]}</td><td class="n">{c["n"]}</td>'
                        f'<td class="d hide">{when.strftime("%Y-%m-%d") if when else "?"}</td></tr>')
        script = ("const q=document.getElementById('q');q.addEventListener('input',()=>{const v=q.value.toLowerCase();"
                  "document.querySelectorAll('tbody tr').forEach(r=>{r.hidden=!r.dataset.s.includes(v)})});")
        total = sum(c["n"] for c in chats)
        (self.out / "index.html").write_text(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Чаты WhatsApp</title>'
            '<link rel="stylesheet" href="style.css"></head><body><header>'
            f'<h1>Чаты WhatsApp: {len(chats)}, сообщений: {total}</h1></header><main>'
            '<input id="q" type="search" placeholder="Поиск по названию чата" aria-label="Поиск по названию чата">'
            '<table><thead><tr><th>Чат</th><th class="hide">Тип</th><th class="n">Сообщений</th><th class="hide">Последнее</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></main><script>{script}</script></body></html>',
            encoding="utf-8")

    def prune(self, chats: list[dict]) -> None:
        """Убирает файлы прошлой выгрузки, если чат с тех пор получил другое имя файла."""
        keep = {c["slug"] for c in chats}
        for sub, suffix in (("html", ".html"), ("txt", ".txt"), ("jsonl", ".jsonl")):
            for path in (self.out / sub).glob(f"*{suffix}"):
                if path.stem not in keep:
                    path.unlink()
        media = self.out / "media"
        if media.is_dir():
            for path in media.iterdir():
                if path.is_dir() and path.name not in keep:
                    shutil.rmtree(path)  # удаляются только ссылки или копии, бэкап не затрагивается

    def run(self, needle: str | None) -> dict:
        chats = self.chat_sessions(needle)
        if not chats:
            fail("чаты не найдены")
        if not needle and (self.out / "summary.json").exists():
            self.prune(chats)
        for index, chat in enumerate(chats, 1):
            self.export_chat(chat)
            if index % 200 == 0 or index == len(chats):
                print(f"{index}/{len(chats)} чатов, {self.stats['messages']} сообщений", flush=True)
        self.write_index(chats)
        (self.out / "summary.json").write_text(json.dumps(self.stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.stats


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Экспорт чатов WhatsApp из незашифрованного бэкапа iPhone")
    parser.add_argument("--backup", help="папка бэкапа; по умолчанию самая свежая найденная")
    parser.add_argument("--out", help="куда сохранить результат; по умолчанию ./output, а с --chat ./output_chat")
    parser.add_argument("--media", choices=["link", "copy", "none"], default="link",
                        help="link: жёсткие ссылки, место почти не занимают (по умолчанию); copy: копии файлов; none: без медиа")
    parser.add_argument("--chat", help="выгрузить только чаты, где название содержит этот текст")
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path.cwd() / ("output_chat" if args.chat else "output")
    backup = find_backup(args.backup)
    exporter = Exporter(backup, out, args.media)
    stats = exporter.run(args.chat)
    print(json.dumps(stats, ensure_ascii=False))
    if exporter.link_fell_back:
        print("Часть файлов скопирована: жёсткие ссылки недоступны между разными дисками.")
    print(f"Готово: {out / 'index.html'}")


if __name__ == "__main__":
    main()
