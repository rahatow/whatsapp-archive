"""Start the local app: python -m whatsapp_archive"""
from __future__ import annotations

import argparse
import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="WhatsApp archive: export, transcribe and search your chats locally")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--data", help="folder for the private archive (default: ./data)")
    args = parser.parse_args()
    if args.data:
        os.environ["WA_ARCHIVE_DATA"] = args.data  # read when the server module is imported below
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [f"http://127.0.0.1:{args.port}"]).start()
    uvicorn.run("whatsapp_archive.server:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
