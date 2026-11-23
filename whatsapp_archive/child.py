"""Runs a heavy export step in a child process that dies together with the app.

The app holds this process's stdin open. When the app exits for any reason, even a crash,
the pipe closes and the child stops, so nothing keeps working in the background.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback


def watch_parent() -> None:
    # os.read holds no Python lock. A buffered read from a daemon thread crashes the
    # interpreter at exit with "could not acquire lock for <_io.BufferedReader name='<stdin>'>".
    try:
        while os.read(0, 1024):
            pass
    except OSError:
        pass
    os._exit(1)


def run_step(mode: str, arguments: list[str]) -> None:
    if mode == "extract":
        from Whatsapp_Chat_Exporter.ios_media_handler import extract_media
        from Whatsapp_Chat_Exporter.utility import WhatsAppIdentifier

        backup, link = arguments
        extract_media(backup, WhatsAppIdentifier, 1024 * 1024, link == "1")
    elif mode == "export":
        from Whatsapp_Chat_Exporter.__main__ import main as exporter_main

        sys.argv = ["wtsexporter", *arguments]
        exporter_main()
    else:
        raise SystemExit(f"Unknown mode: {mode}")


def main() -> None:
    threading.Thread(target=watch_parent, daemon=True).start()
    code = 0
    try:
        run_step(sys.argv[1], sys.argv[2:])
    except SystemExit as error:
        if error.code is None:
            code = 0
        elif isinstance(error.code, int):
            code = error.code
        else:
            print(error.code, file=sys.stderr)
            code = 1
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # skips interpreter shutdown, which the watcher thread must not take part in


if __name__ == "__main__":
    main()
