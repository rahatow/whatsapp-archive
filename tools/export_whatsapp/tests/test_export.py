"""Проверка на синтетическом бэкапе: реальные переписки в тестах не используются.

    python tests/test_export.py [папка]

С указанной папкой оставляет там результат, чтобы посмотреть вёрстку.
"""
import json
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from export_whatsapp import DOMAIN, is_blob, reply_id, slugify  # noqa: E402

CHAT_ID = "a1" * 20
CONTACT_ID = "b2" * 20
LID_ID = "c3" * 20
PHOTO_ID = "d4" * 20
EPOCH_2025 = 757382400  # 2025-01-01 в секундах от 2001-01-01


def table(conn, name, columns):
    conn.execute(f"CREATE TABLE {name} (Z_PK INTEGER PRIMARY KEY, {', '.join(columns)})")


def put(backup, file_id, payload=b""):
    path = backup / file_id[:2] / file_id
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload:
        path.write_bytes(payload)
    return path


def make_backup(backup: Path, encrypted=False):
    backup.mkdir(parents=True)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted}))
    manifest = sqlite3.connect(backup / "Manifest.db")
    manifest.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INT)")
    manifest.executemany("INSERT INTO Files VALUES (?,?,?,1)", [
        (CHAT_ID, DOMAIN, "ChatStorage.sqlite"), (CONTACT_ID, DOMAIN, "ContactsV2.sqlite"),
        (LID_ID, DOMAIN, "LID.sqlite"), (PHOTO_ID, DOMAIN, "Message/Media/1/photo.jpg"),
    ])
    manifest.commit()
    manifest.close()
    put(backup, PHOTO_ID, b"\xff\xd8\xff\xd9")

    contacts = sqlite3.connect(put(backup, CONTACT_ID))
    table(contacts, "ZWAADDRESSBOOKCONTACT", ["ZWHATSAPPID", "ZLID", "ZFULLNAME", "ZBUSINESSNAME"])
    contacts.execute("INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1,'491111@s.whatsapp.net','777@lid','Анна Тест',NULL)")
    contacts.commit()
    contacts.close()

    lid = sqlite3.connect(put(backup, LID_ID))
    table(lid, "ZWAZACCOUNT", ["ZIDENTIFIER", "ZPHONENUMBER", "ZDISPLAYNAME"])
    lid.execute("INSERT INTO ZWAZACCOUNT VALUES (1,'888@lid','492222',NULL)")
    lid.commit()
    lid.close()

    db = sqlite3.connect(put(backup, CHAT_ID))
    table(db, "ZWACHATSESSION", ["ZSESSIONTYPE", "ZCONTACTJID", "ZPARTNERNAME"])
    table(db, "ZWAGROUPMEMBER", ["ZMEMBERJID", "ZCONTACTNAME", "ZFIRSTNAME"])
    table(db, "ZWAPROFILEPUSHNAME", ["ZJID", "ZPUSHNAME"])
    table(db, "ZWAMEDIAITEM", ["ZMEDIALOCALPATH", "ZTITLE", "ZLATITUDE", "ZLONGITUDE", "ZVCARDNAME", "ZMETADATA", "ZMOVIEDURATION"])
    table(db, "ZWAMESSAGE", ["ZCHATSESSION", "ZMESSAGEDATE", "ZISFROMME", "ZMESSAGETYPE", "ZTEXT", "ZSTANZAID", "ZSTARRED",
                             "ZFROMJID", "ZPUSHNAME", "ZGROUPMEMBER", "ZMEDIAITEM"])
    db.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?,?)", [
        (1, 0, "491111@s.whatsapp.net", "+49 1111"), (2, 1, "123-456@g.us", 'Группа: "тест"/1 <img src=x onerror=alert(1)>'),
        (3, 0, "999@s.whatsapp.net", "Пустой чат"),
    ])
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (1,'888@lid','+49 2222','+GAAgAA==')")
    db.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES (1,'888@lid','Профиль')")
    reply = b"\x2a\x05abc01"
    db.executemany("INSERT INTO ZWAMEDIAITEM VALUES (?,?,?,?,?,?,?,?)", [
        (1, "Media/1/photo.jpg", "Подпись к фото", 0.0, 0.0, "hash", None, None),
        (2, "Media/1/gone.opus", None, None, None, None, None, 65.0),
        (3, None, None, None, None, None, reply, None),
        (4, None, "deadbeef-id", None, None, None, None, None),
        (5, None, None, 52.5, 13.4, None, None, None),
    ])
    d = EPOCH_2025
    db.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, 1, d, 0, 0, "Привет <b>https://example.com/?a=1&b=2</b>", "abc01", 0, "491111@s.whatsapp.net", None, None, None),
        (2, 1, d + 60, 1, 0, "Ответ", "abc02", 1, None, None, None, 3),
        (3, 1, d + 120, 1, 1, "", "abc03", 0, None, None, None, 1),
        (4, 1, d + 180, 0, 3, "", "abc04", 0, None, None, None, 2),
        (5, 1, d + 240, 0, 14, "", "abc05", 0, None, None, None, 4),
        (6, 1, d + 300, 0, 5, "", "abc06", 0, None, None, None, 5),
        (7, 1, d + 360, 0, 99, "Текст неизвестного типа", "abc07", 0, None, None, None, None),
        (8, 1, d + 420, 0, 10, None, "abc08", 0, None, None, None, None),
        (12, 1, d + 480, 1, 59, None, "abc12", 0, None, None, None, None),
        (13, 1, d + 540, 0, 66, None, "abc13", 0, None, None, None, None),
        (14, 1, d + 600, 0, 12, None, "abc14", 0, None, None, None, None),
        (15, 1, d + 660, 0, 75, None, "abc15", 0, None, None, None, None),
        (9, 2, d + 86400, 0, 0, "Из группы", "abc09", 0, "123-456@g.us", "+GAAgAA==", 1, None),
        (10, 2, d + 86460, 0, 6, "Участник добавлен", "abc10", 0, None, None, None, None),
        (11, 2, -978307000.0, 0, 0, "Без даты", "abc11", 0, "123-456@g.us", None, 1, None),
    ])
    db.commit()
    db.close()


def run(*args):
    return subprocess.run([sys.executable, str(ROOT / "export_whatsapp.py"), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def main(keep: Path | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        backup, out = tmp / "backup", keep or tmp / "out"
        make_backup(backup)
        before = sorted((p.relative_to(backup), p.read_bytes()) for p in backup.rglob("*") if p.is_file())
        result = run("--backup", str(backup), "--out", str(out))
        assert result.returncode == 0, result.stderr
        after = sorted((p.relative_to(backup), p.read_bytes()) for p in backup.rglob("*") if p.is_file())
        assert before == after, "бэкап изменился"

        stats = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert stats["chats"] == 2 and stats["messages"] == 15, stats
        assert stats["media_exported"] == 1 and stats["media_not_in_backup"] == 1, stats

        personal = next((out / "txt").glob("Анна*.txt")).read_text(encoding="utf-8")
        assert "Я: Ответ" in personal
        assert "[голосовое 1:05, файла нет в бэкапе]" in personal
        assert "deadbeef" not in personal
        assert "[тип 99] Текст неизвестного типа" in personal
        assert "Подпись к фото" in personal
        assert "Я: [звонок]" in personal
        assert "[альбом]" in personal and "[возможно, звонок]" in personal
        assert "[удалено или изменено]" in personal
        assert "тип 75" not in personal, "пустая служебная запись должна быть скрыта"
        records = next((out / "jsonl").glob("Анна*.jsonl")).read_text(encoding="utf-8").splitlines()
        assert any(json.loads(x)["type_code"] == 75 for x in records), "в jsonl запись должна остаться"

        page = next((out / "html").glob("Анна*.html")).read_text(encoding="utf-8")
        assert "&lt;b&gt;" in page and "<b>https" not in page
        assert 'class="quote" href="#m1"' in page
        assert "<img" in page and "openstreetmap" in page

        group_file = next((out / "jsonl").glob("Группа*.jsonl"))
        assert '"' not in group_file.name and "/" not in group_file.name
        group = [json.loads(x) for x in group_file.read_text(encoding="utf-8").splitlines()]
        assert group[0]["time"] is None, "дата до 1970 не должна ронять экспорт"
        assert group[1]["sender"] == "Профиль", group[1]["sender"]
        assert any(x["type"] == "system" for x in group)
        group_page = next((out / "html").glob("Группа*.html")).read_text(encoding="utf-8")
        assert "Участник добавлен" in group_page
        assert "<img src=x" not in group_page, "название чата должно экранироваться"
        assert "<img src=x" not in (out / "index.html").read_text(encoding="utf-8")

        media = list((out / "media").rglob("*photo.jpg"))
        assert len(media) == 1 and media[0].read_bytes() == b"\xff\xd8\xff\xd9"

        (out / "txt" / "старое имя_9.txt").write_text("устаревший файл", encoding="utf-8")
        rerun = run("--backup", str(backup), "--out", str(out))
        assert rerun.returncode == 0, rerun.stderr
        assert not (out / "txt" / "старое имя_9.txt").exists(), "устаревший файл не удалён"

        filtered = tmp / "filtered"
        result = run("--backup", str(backup), "--out", str(filtered), "--chat", "анна", "--media", "none")
        assert result.returncode == 0, result.stderr
        assert len(list((filtered / "txt").iterdir())) == 1 and not (filtered / "media").exists()

        encrypted = tmp / "encrypted"
        make_backup(encrypted, encrypted=True)
        result = run("--backup", str(encrypted), "--out", str(tmp / "e"))
        assert result.returncode != 0 and "зашифрован" in result.stderr

        assert is_blob("+GAAgAA==") and is_blob("CNj5sdcGIABIAZABAPABAtgClvqXoP")
        assert not is_blob("Vasyap13") and not is_blob("+4915112345") and not is_blob("Анна")
        assert reply_id(b"\x08\x01\x2a\x03xyz") == "xyz"
        assert reply_id(b"\xa2\x04\x01a") is None
        assert reply_id(b"\x2a\xff") in (None, "")
        assert slugify("CON", 1) == "CON_1" and slugify("a/b:c", 2) == "a_b_c_2"
    print("OK")


def test_export():
    main()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
