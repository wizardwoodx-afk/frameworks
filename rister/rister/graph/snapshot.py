"""Durable snapshots for graph runs.

A snapshot is taken after every superstep. It captures the merged state, the
frontier that is due to execute next, and any pending interrupt payload. Because
snapshots are content-complete, a run can be resumed on another process or
replayed step by step for debugging.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..core.errors import NotFound

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_INTERRUPTED = "interrupted"


@dataclass
class Snapshot:
    """A point-in-time view of one thread."""

    thread_id: str
    step: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    next: list[str] = field(default_factory=list)
    status: str = STATUS_RUNNING
    interrupt: Any = None
    created: float = field(default_factory=time.time)
    fingerprint: str | None = None

    @property
    def done(self) -> bool:
        return self.status == STATUS_OK

    @property
    def interrupted(self) -> bool:
        return self.status == STATUS_INTERRUPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "step": self.step,
            "values": self.values,
            "next": list(self.next),
            "status": self.status,
            "interrupt": self.interrupt,
            "created": self.created,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Snapshot":
        return cls(
            thread_id=data["thread_id"],
            step=int(data.get("step", 0)),
            values=dict(data.get("values") or {}),
            next=list(data.get("next") or []),
            status=data.get("status", STATUS_RUNNING),
            interrupt=data.get("interrupt"),
            created=float(data.get("created", time.time())),
            fingerprint=data.get("fingerprint"),
        )


class SnapshotStore:
    """Interface every persistence backend implements."""

    def save(self, snapshot: Snapshot) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def load(self, thread_id: str) -> Snapshot | None:  # pragma: no cover
        raise NotImplementedError

    def history(self, thread_id: str) -> list[Snapshot]:  # pragma: no cover
        raise NotImplementedError

    def threads(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError

    def clear(self, thread_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    def require(self, thread_id: str) -> Snapshot:
        snap = self.load(thread_id)
        if snap is None:
            raise NotFound(f"no snapshot recorded for thread {thread_id!r}")
        return snap


class MemorySnapshotStore(SnapshotStore):
    """Thread-safe in-process store that keeps the full snapshot history."""

    def __init__(self) -> None:
        self._threads: dict[str, list[Snapshot]] = {}
        self._lock = threading.RLock()

    def save(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._threads.setdefault(snapshot.thread_id, []).append(snapshot)

    def load(self, thread_id: str) -> Snapshot | None:
        with self._lock:
            entries = self._threads.get(thread_id)
            return entries[-1] if entries else None

    def history(self, thread_id: str) -> list[Snapshot]:
        with self._lock:
            return list(self._threads.get(thread_id, []))

    def threads(self) -> list[str]:
        with self._lock:
            return sorted(self._threads)

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)


class FileSnapshotStore(SnapshotStore):
    """Append-only JSON-lines history per thread, crash-safe on write.

    Each ``<thread>.jsonl`` file holds one snapshot per line; the last line is
    the current state and earlier lines are the replay history.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- internals ---------------------------------------------------------
    def _path(self, thread_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)
        return self.root / f"{safe}.jsonl"

    # -- api ---------------------------------------------------------------
    def save(self, snapshot: Snapshot) -> None:
        payload = json.dumps(snapshot.to_dict(), sort_keys=True, default=str)
        path = self._path(snapshot.thread_id)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    if path.exists():
                        fh.write(path.read_text(encoding="utf-8"))
                    fh.write(payload + "\n")
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):  # pragma: no cover - defensive
                    os.unlink(tmp)

    def history(self, thread_id: str) -> list[Snapshot]:
        path = self._path(thread_id)
        if not path.exists():
            return []
        out: list[Snapshot] = []
        with self._lock:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Snapshot.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):  # pragma: no cover
                    continue
        return out

    def load(self, thread_id: str) -> Snapshot | None:
        entries = self.history(thread_id)
        return entries[-1] if entries else None

    def threads(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    def clear(self, thread_id: str) -> None:
        path = self._path(thread_id)
        with self._lock:
            if path.exists():
                path.unlink()


__all__ = [
    "STATUS_RUNNING",
    "STATUS_OK",
    "STATUS_INTERRUPTED",
    "Snapshot",
    "SnapshotStore",
    "MemorySnapshotStore",
    "FileSnapshotStore",
]
