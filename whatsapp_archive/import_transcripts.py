"""Import voice transcripts made earlier, from a text export of one chat.

Expected lines:
    [2024-04-13 15:38] Alex: [voice A0001 @2024-04-13 15:38:20: export/alex/...opus]
        [voice transcript (lang=ru ... src=...)]: text

Each transcript is matched to a voice message of the chosen chat by its exact second and by
the sender (you or the other person). Only unambiguous matches are imported, and finished
transcripts already in the archive are never overwritten. Pending entries are skipped.

    python -m whatsapp_archive.import_transcripts FILE --chat JID --me "Your name" [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .store import connect

VOICE_LINE = re.compile(r"^\[[^\]]+\] (?P<sender>[^:]+): \[voice \w+ @(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d):")
TRANSCRIPT_LINE = re.compile(r"^\s+\[voice transcript(?: \((?P<params>[^)]*)\))?\]: (?P<text>.*)$")
SKIP_TEXT = {"", "(pending)"}


@dataclass
class Entry:
    when: datetime
    from_me: bool
    text: str
    language: str | None
    source: str


def parse(path: Path, me: str) -> list[Entry]:
    entries: list[Entry] = []
    current = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            voice = VOICE_LINE.match(line)
            if voice:
                current = (datetime.strptime(voice["when"], "%Y-%m-%d %H:%M:%S"), voice["sender"].strip() == me)
                continue
            transcript = TRANSCRIPT_LINE.match(line)
            if transcript and current:
                text = transcript["text"].strip()
                if text not in SKIP_TEXT:
                    params = dict(re.findall(r"(\w+)=(\S+)", transcript["params"] or ""))
                    entries.append(Entry(current[0], current[1], text, params.get("lang"),
                                         params.get("src", "earlier export")))
                current = None
            elif not line.startswith((" ", "\t")):
                current = None
    return entries


def candidates(chat: str) -> dict[tuple[int, bool], list[str]]:
    """Voice files of the chat keyed by local time in whole seconds and by sender."""
    found: dict[tuple[int, bool], list[str]] = {}
    with connect() as db:
        rows = db.execute("SELECT DISTINCT media_path, ts, from_me FROM messages "
                          "WHERE chat_jid=? AND mime LIKE 'audio/%' AND media_path IS NOT NULL", (chat,))
        for row in rows:
            found.setdefault((int(round(row["ts"])), bool(row["from_me"])), []).append(row["media_path"])
    return found


def match(entries: list[Entry], found: dict[tuple[int, bool], list[str]]) -> tuple[int, dict[Entry, str]]:
    """Finds the hour offset between the two exports and matches within 2 seconds."""
    def at(entry: Entry, offset: int) -> list[str]:
        base = int(entry.when.timestamp()) + offset * 3600
        paths: list[str] = []
        for delta in (0, -1, 1, -2, 2):
            paths = found.get((base + delta, entry.from_me), [])
            if paths:
                break
        return paths

    sample = entries[:300]
    offset = max(range(-14, 15), key=lambda hours: sum(len(at(entry, hours)) == 1 for entry in sample))
    matched: dict[Entry, str] = {}
    used: Counter[str] = Counter()
    for entry in entries:
        paths = at(entry, offset)
        if len(paths) == 1:
            matched[id(entry)] = paths[0]
            used[paths[0]] += 1
    # A file claimed by two transcripts is ambiguous, so neither is imported.
    return offset, {key: path for key, path in matched.items() if used[path] == 1}


def run(path: Path, chat: str, me: str, dry_run: bool = False) -> dict:
    entries = parse(path, me)
    offset, matched = match(entries, candidates(chat))
    by_id = {id(entry): entry for entry in entries}
    imported = kept = 0
    with connect() as db:
        for key, media_path in matched.items():
            existing = db.execute("SELECT status FROM transcripts WHERE media_path=?", (media_path,)).fetchone()
            if existing and existing["status"] == "done":
                kept += 1
                continue
            if not dry_run:
                entry = by_id[key]
                db.execute(
                    "INSERT OR REPLACE INTO transcripts(media_path,status,text,language,backend,model,error,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (media_path, "done", entry.text, entry.language, "import", entry.source, None, time.time()))
            imported += 1
    return {"transcripts_in_file": len(entries), "hour_offset": offset, "matched": len(matched),
            "imported": imported, "already_done": kept, "unmatched": len(entries) - len(matched), "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", type=Path)
    parser.add_argument("--chat", required=True, help="chat id from the archive, for example 49...@s.whatsapp.net")
    parser.add_argument("--me", required=True, help="your name as written in the file")
    parser.add_argument("--dry-run", action="store_true", help="only count matches")
    args = parser.parse_args()
    print(run(args.file, args.chat, args.me, args.dry_run))


if __name__ == "__main__":
    main()
