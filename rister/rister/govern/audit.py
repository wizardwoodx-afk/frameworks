"""Tamper-evident audit log.

Each entry commits to the previous entry's digest, so removing or editing any
record invalidates the chain from that point on. Entries can additionally be
signed with an HMAC key, which makes forgery require the key rather than just
recomputation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

GENESIS = "0" * 64


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(prev: str, payload: Mapping[str, Any], key: bytes | None = None) -> str:
    """Hash (or HMAC) the previous digest together with this entry."""
    body = f"{prev}|{_canonical(payload)}".encode("utf-8")
    if key:
        return hmac.new(key, body, hashlib.sha256).hexdigest()
    return hashlib.sha256(body).hexdigest()


@dataclass
class AuditEntry:
    """One recorded governance decision."""

    seq: int
    action: str
    effect: str
    actor: str | None = None
    rule: str | None = None
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    arguments_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    prev: str = GENESIS
    hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": round(self.timestamp, 6),
            "actor": self.actor,
            "action": self.action,
            "effect": self.effect,
            "rule": self.rule,
            "reason": self.reason,
            "arguments_digest": self.arguments_digest,
            "metadata": self.metadata,
        }

    def to_dict(self) -> dict[str, Any]:
        out = self.body()
        out["prev"] = self.prev
        out["hash"] = self.hash
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditEntry":
        return cls(
            seq=int(data["seq"]),
            action=data.get("action", ""),
            effect=data.get("effect", ""),
            actor=data.get("actor"),
            rule=data.get("rule"),
            reason=data.get("reason", ""),
            timestamp=float(data.get("timestamp", 0.0)),
            arguments_digest=data.get("arguments_digest", ""),
            metadata=dict(data.get("metadata") or {}),
            prev=data.get("prev", GENESIS),
            hash=data.get("hash", ""),
        )


@dataclass
class Verification:
    """Outcome of checking a chain."""

    ok: bool
    checked: int = 0
    broken_at: int | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "broken_at": self.broken_at,
            "reason": self.reason,
        }


class AuditLog:
    """An append-only, hash-chained log held in memory."""

    def __init__(self, key: str | bytes | None = None, name: str = "audit") -> None:
        self.key = key.encode("utf-8") if isinstance(key, str) else key
        self.name = name
        self.entries: list[AuditEntry] = []
        self._lock = threading.RLock()

    # -- writing -----------------------------------------------------------
    def record(
        self,
        action: str,
        effect: str,
        *,
        actor: str | None = None,
        rule: str | None = None,
        reason: str = "",
        arguments: Any = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        with self._lock:
            seq = len(self.entries) + 1
            prev = self.entries[-1].hash if self.entries else GENESIS
            args_digest = (
                hashlib.sha256(_canonical({"arguments": arguments}).encode("utf-8")).hexdigest()
                if arguments is not None
                else ""
            )
            entry = AuditEntry(
                seq=seq,
                action=action,
                effect=effect,
                actor=actor,
                rule=rule,
                reason=reason,
                arguments_digest=args_digest,
                metadata=dict(metadata or {}),
                prev=prev,
            )
            entry.hash = digest(prev, entry.body(), self.key)
            self.entries.append(entry)
            return entry

    def extend(self, entries: Iterable[AuditEntry]) -> None:
        with self._lock:
            for entry in entries:
                self.entries.append(entry)

    # -- verification ------------------------------------------------------
    def verify(self) -> Verification:
        with self._lock:
            prev = GENESIS
            for index, entry in enumerate(self.entries):
                expected = digest(prev, entry.body(), self.key)
                if entry.prev != prev:
                    return Verification(False, index, entry.seq, "prev digest mismatch")
                if not hmac.compare_digest(expected, entry.hash):
                    return Verification(False, index, entry.seq, "entry digest mismatch")
                prev = entry.hash
            return Verification(True, len(self.entries))

    # -- reading -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def find(self, action: str | None = None, effect: str | None = None) -> list[AuditEntry]:
        return [
            e
            for e in self.entries
            if (action is None or e.action == action) and (effect is None or e.effect == effect)
        ]

    def denied(self) -> list[AuditEntry]:
        return self.find(effect="deny")

    def to_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]

    def to_jsonl(self) -> str:
        return "\n".join(_canonical(e.to_dict()) for e in self.entries)

    # -- persistence -------------------------------------------------------
    def save(self, path: str | os.PathLike[str]) -> Path:
        target = Path(path)
        target.write_text(self.to_jsonl() + ("\n" if self.entries else ""), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str], key: str | bytes | None = None) -> "AuditLog":
        log = cls(key=key)
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line:
                log.entries.append(AuditEntry.from_dict(json.loads(line)))
        return log


class FileAuditLog(AuditLog):
    """An audit log that also appends every entry to a JSON-lines file."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        key: str | bytes | None = None,
        name: str = "audit",
        load_existing: bool = True,
    ) -> None:
        super().__init__(key=key, name=name)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if load_existing and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        self.entries.append(AuditEntry.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError):  # pragma: no cover
                        continue

    def record(self, *args: Any, **kwargs: Any) -> AuditEntry:
        entry = super().record(*args, **kwargs)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(entry.to_dict()) + "\n")
        return entry


__all__ = [
    "GENESIS",
    "digest",
    "AuditEntry",
    "AuditLog",
    "FileAuditLog",
    "Verification",
]
