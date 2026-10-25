"""Conversation memory.

Two layers, deliberately separated:

* a **store** (:class:`MemoryStore`) persists messages per thread;
* a **memory** policy (:class:`Memory`) decides what the model actually sees —
  a sliding window, a token budget, or a rolling summary of older turns.

Everything works offline and without a database; ``FileMemoryStore`` gives you
durability across processes using JSON lines.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..core.types import Message, Role


# --------------------------------------------------------------------------
# stores
# --------------------------------------------------------------------------
class MemoryStore:
    """Persistence interface for message history."""

    def append(self, thread_id: str, messages: Sequence[Message]) -> None:  # pragma: no cover
        raise NotImplementedError

    def read(self, thread_id: str) -> list[Message]:  # pragma: no cover
        raise NotImplementedError

    def clear(self, thread_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def threads(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError


class MemoryStoreBase(MemoryStore):
    """Shared helpers."""

    def extend(self, thread_id: str, *messages: Message) -> None:
        self.append(thread_id, list(messages))


class InMemoryStore(MemoryStoreBase):
    """Process-local store."""

    def __init__(self) -> None:
        self._data: dict[str, list[Message]] = {}
        self._lock = threading.RLock()

    def append(self, thread_id: str, messages: Sequence[Message]) -> None:
        with self._lock:
            bucket = self._data.setdefault(thread_id, [])
            bucket.extend(messages)

    def read(self, thread_id: str) -> list[Message]:
        with self._lock:
            return list(self._data.get(thread_id, []))

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._data.pop(thread_id, None)

    def threads(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


class FileMemoryStore(MemoryStoreBase):
    """One JSON-lines file per thread; append-only and crash-safe."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, thread_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in thread_id)
        return self.root / f"{safe}.jsonl"

    def append(self, thread_id: str, messages: Sequence[Message]) -> None:
        path = self._path(thread_id)
        with self._lock, path.open("a", encoding="utf-8") as fh:
            for message in messages:
                fh.write(json.dumps(message.to_dict(), default=str) + "\n")

    def read(self, thread_id: str) -> list[Message]:
        path = self._path(thread_id)
        if not path.exists():
            return []
        out: list[Message] = []
        with self._lock:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(Message.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError, ValueError):  # pragma: no cover
                    continue
        return out

    def clear(self, thread_id: str) -> None:
        path = self._path(thread_id)
        with self._lock:
            if path.exists():
                path.unlink()

    def threads(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))


# --------------------------------------------------------------------------
# memory policies
# --------------------------------------------------------------------------
@dataclass
class Memory:
    """Decide which remembered messages are fed back to the model."""

    store: MemoryStore
    window: int | None = None
    include_system: bool = True

    # -- read/write --------------------------------------------------------
    def history(self, thread_id: str) -> list[Message]:
        messages = self.store.read(thread_id)
        system = [m for m in messages if m.role is Role.SYSTEM] if self.include_system else []
        rest = [m for m in messages if m.role is not Role.SYSTEM]
        if self.window is not None and self.window >= 0:
            rest = rest[-self.window :] if self.window else []
        return system + rest

    def append(self, thread_id: str, *messages: Message) -> None:
        self.store.append(thread_id, list(messages))

    def clear(self, thread_id: str) -> None:
        self.store.clear(thread_id)

    def threads(self) -> list[str]:
        return self.store.threads()

    def __len__(self) -> int:
        return sum(len(self.store.read(t)) for t in self.store.threads())


class SummarizingMemory(Memory):
    """Compress older turns into a rolling summary once history grows.

    Summaries are produced by a model, but the *policy* stays deterministic: the
    threshold and the number of recent messages kept verbatim are explicit.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        window: int | None = 12,
        summarize_after: int = 20,
        model: Any = None,
        summary_key: str = "summary",
    ) -> None:
        super().__init__(store=store, window=window)
        self.summarize_after = summarize_after
        self.model = model
        self.summary_key = summary_key
        self._summaries: dict[str, str] = {}

    def _summarise(self, messages: Sequence[Message], previous: str) -> str:
        transcript = "\n".join(f"{m.role.value}: {m.text}" for m in messages if m.text)
        if self.model is None:
            # Deterministic fallback: keep a compact digest without a model call.
            return (previous + "\n" if previous else "") + transcript[:2000]
        prompt = [
            Message.system(
                "Summarise the conversation so far. Be factual and terse; "
                "keep names, numbers, decisions and open questions."
            ),
            Message.user(
                (f"Existing summary:\n{previous}\n\n" if previous else "")
                + f"Conversation:\n{transcript}"
            ),
        ]
        completion = self.model.complete(prompt)
        return completion.text.strip()

    def history(self, thread_id: str) -> list[Message]:
        messages = self.store.read(thread_id)
        system = [m for m in messages if m.role is Role.SYSTEM] if self.include_system else []
        rest = [m for m in messages if m.role is not Role.SYSTEM]

        if len(rest) > self.summarize_after:
            older = rest[: len(rest) - (self.window or 0)]
            if older:
                self._summaries[thread_id] = self._summarise(
                    older, self._summaries.get(thread_id, "")
                )
                rest = rest[len(older) :]

        if self.window is not None and self.window >= 0:
            rest = rest[-self.window :] if self.window else []

        out = list(system)
        summary = self._summaries.get(thread_id)
        if summary:
            out.append(
                Message.system(f"Summary of earlier conversation:\n{summary}", kind="summary")
            )
        return out + rest

    def summary(self, thread_id: str) -> str:
        return self._summaries.get(thread_id, "")


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent token estimate (about 4 characters/token)."""
    return max(1, (len(text) + 3) // 4)


__all__ = [
    "MemoryStore",
    "InMemoryStore",
    "FileMemoryStore",
    "Memory",
    "SummarizingMemory",
    "estimate_tokens",
]
