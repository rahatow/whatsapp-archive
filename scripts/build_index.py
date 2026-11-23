"""Index for search over all chats, from the command line (same as the «Индекс» button in the app).

  --chunks-only  rebuild chunks and full-text search only (CPU, a few minutes)
  (no flag)      chunks + bge-m3 embeddings for every chunk that has none (GPU)

"""
import argparse
import os
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from whatsapp_archive import rag  # noqa: E402
from whatsapp_archive.jobs import JobManager  # noqa: E402

def report(job: JobManager, stop: threading.Event) -> None:
    while not stop.wait(60):
        p = job.progress.as_dict()
        eta = f", осталось ~{p['eta'] / 60:.0f} мин" if p.get("eta") else ""
        print(f"{time.strftime('%H:%M')} {p['stage']}: {p['done']}/{p['total']}{eta}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks-only", action="store_true")
    args = parser.parse_args()
    if args.chunks_only:
        print("фрагментов:", rag.build_chunks(), flush=True)
        print(rag.stats())
        return
    job = JobManager()
    stop = threading.Event()
    threading.Thread(target=report, args=(job, stop), daemon=True).start()
    started = time.time()
    try:
        note = rag.index(job)
        print(note, f"за {(time.time() - started) / 60:.0f} мин", flush=True)
        print(rag.stats())
    finally:
        stop.set()


if __name__ == "__main__":
    main()
