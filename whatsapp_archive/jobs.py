"""One background job at a time, so the GPU and the database are never fought over."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

log = logging.getLogger("whatsapp_archive")


class Cancelled(Exception):
    pass


@dataclass
class Progress:
    kind: str = ""
    state: str = "idle"  # idle | running | done | failed | cancelled
    stage: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    note: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    work_started_at: float = field(default=0.0)

    def as_dict(self) -> dict:
        data = asdict(self)
        now = self.finished_at or time.time()
        data["elapsed"] = now - self.started_at if self.started_at else 0
        finished = self.done + self.failed
        rate_elapsed = now - (self.work_started_at or self.started_at) if self.started_at else 0
        data["eta"] = (
            rate_elapsed / finished * (self.total - finished)
            if self.state == "running" and finished and self.total > finished else None
        )
        return data


class JobManager:
    def __init__(self) -> None:
        self.progress = Progress()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str, target: Callable[["JobManager"], str | None]) -> bool:
        with self._lock:
            if self.busy:
                return False
            self._cancel.clear()
            self.progress = Progress(kind=kind, state="running", started_at=time.time())
            self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
            self._thread.start()
            return True

    def _run(self, target: Callable[["JobManager"], str | None]) -> None:
        try:
            note = target(self)
            self.progress.state = "done"
            if note:
                self.progress.note = note
        except Cancelled:
            self.progress.state = "cancelled"
        except Exception as error:  # the UI must show every failure instead of hanging
            log.exception("Job failed")
            self.progress.state = "failed"
            self.progress.note = f"{type(error).__name__}: {error}"[:500]
        self.progress.finished_at = time.time()

    def cancel(self) -> None:
        self._cancel.set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def stage(self, stage: str, total: int = 0) -> None:
        self.check()
        self.progress.stage = stage
        self.progress.total = total
        self.progress.done = 0
        self.progress.failed = 0
        self.progress.work_started_at = time.time()
