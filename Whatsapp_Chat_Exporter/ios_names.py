"""Display names for group members on iOS, including members known only by an internal @lid id.

Priority: address book, name stored for the group member, profile name, phone number.
"""
import os
import re
import sqlite3


def _digits(jid):
    return re.sub(r"\D", "", (jid or "").split("@")[0].split(":")[0])


def _is_phone_like(value):
    return not re.sub(r"[\s+\-()\d‑•‎‏‪-‮⁦-⁩]", "", value)


def _is_blob(value):
    """WhatsApp keeps base64 service data in some name columns. It is not a name."""
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        return False
    return (value.startswith("+") and not _is_phone_like(value)) or len(value) >= 20


def _open(path):
    if not os.path.isfile(path):
        return None
    uri = "file:" + os.path.abspath(path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


class SenderNames:
    def __init__(self, chat_db, media_folder):
        self.book_phone = {}
        self.book_lid = {}
        self.lid_phone = {}
        self.lid_display = {}
        self.push = {}
        self._load_contacts(os.path.join(media_folder or "", "ContactsV2.sqlite"))
        self._load_lid(os.path.join(media_folder or "", "LID.sqlite"))
        try:
            for jid, name in chat_db.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME").fetchall():
                if jid and name:
                    self.push[_digits(jid)] = name
        except sqlite3.Error:
            pass

    def _load_contacts(self, path):
        conn = _open(path)
        if conn is None:
            return
        try:
            for jid, lid, full, business in conn.execute(
                    "SELECT ZWHATSAPPID, ZLID, ZFULLNAME, ZBUSINESSNAME FROM ZWAADDRESSBOOKCONTACT"):
                name = full or business
                if name and jid:
                    self.book_phone[_digits(jid)] = name
                if name and lid:
                    self.book_lid[_digits(lid)] = name
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _load_lid(self, path):
        conn = _open(path)
        if conn is None:
            return
        try:
            for identifier, phone, display in conn.execute(
                    "SELECT ZIDENTIFIER, ZPHONENUMBER, ZDISPLAYNAME FROM ZWAZACCOUNT"):
                key = _digits(identifier)
                if phone:
                    self.lid_phone[key] = _digits(phone)
                if display:
                    self.lid_display[key] = display
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def resolve(self, jid, *hints):
        hints = [hint for hint in hints if hint and not _is_blob(hint)]
        key = _digits(jid)
        is_lid = (jid or "").endswith("@lid")
        phone = self.lid_phone.get(key, "") if is_lid else key
        book = (self.book_lid.get(key) if is_lid else None) or self.book_phone.get(phone)
        if book:
            return book
        for hint in hints:
            if not _is_phone_like(hint):
                return hint
        named = self.push.get(key) or self.push.get(phone) or self.lid_display.get(key)
        if named:
            return named
        if phone:
            return "+" + phone
        return hints[0] if hints else None
