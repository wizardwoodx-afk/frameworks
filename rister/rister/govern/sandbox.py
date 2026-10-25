"""Execution rings and a filesystem sandbox.

Two independent controls:

* :class:`Ring` — a coarse capability tier (0 trusted .. 3 untrusted) that says
  what an actor may do at all.
* :class:`Sandbox` — a concrete guard for filesystem access: a root jail that
  refuses path traversal, optional symlink escapes, size limits, and an explicit
  switch for subprocess execution (off by default).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..core.errors import SandboxError


class Ring(IntEnum):
    """Capability tiers, loosest to strictest."""

    ROOT = 0      # full access; reserved for the host process
    TRUSTED = 1   # write inside its sandbox, spawn vetted subprocesses
    LIMITED = 2   # read-only inside its sandbox, no subprocesses
    UNTRUSTED = 3 # no filesystem, no subprocesses; pure computation

    @property
    def can_write(self) -> bool:
        return self <= Ring.TRUSTED

    @property
    def can_read(self) -> bool:
        return self <= Ring.LIMITED

    @property
    def can_exec(self) -> bool:
        return self <= Ring.TRUSTED


_PERMISSIONS: dict[Ring, set[str]] = {
    Ring.ROOT: {"read", "write", "exec", "net", "delegate"},
    Ring.TRUSTED: {"read", "write", "exec", "delegate"},
    Ring.LIMITED: {"read"},
    Ring.UNTRUSTED: set(),
}


@dataclass
class Sandbox:
    """A rooted filesystem jail with explicit capabilities."""

    root: str | os.PathLike[str]
    ring: Ring = Ring.LIMITED
    max_bytes: int = 2_000_000
    allow_subprocess: bool = False
    extensions: tuple[str, ...] = ()
    denied_segments: tuple[str, ...] = (".git", ".ssh", "node_modules", "__pycache__")

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.allow_subprocess and not self.ring.can_exec:
            raise SandboxError(
                f"ring {self.ring.name} cannot spawn subprocesses; "
                "construct the sandbox with ring=Ring.TRUSTED instead"
            )

    # -- capabilities ------------------------------------------------------
    def allows(self, permission: str) -> bool:
        return permission in _PERMISSIONS[self.ring]

    def require(self, permission: str) -> None:
        if not self.allows(permission):
            raise SandboxError(
                f"ring {self.ring.name} does not grant {permission!r} "
                f"(allowed: {sorted(_PERMISSIONS[self.ring]) or 'none'})"
            )

    # -- path handling -----------------------------------------------------
    def resolve(self, path: str | os.PathLike[str], *, for_write: bool = False) -> Path:
        """Resolve ``path`` inside the jail, refusing escapes."""
        self.require("write" if for_write else "read")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate

        # Reject traversal and symlink escapes by comparing resolved paths.
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise SandboxError(
                f"path {str(path)!r} escapes the sandbox root {self.root}"
            ) from None

        parts = set(resolved.relative_to(self.root).parts)
        blocked = parts.intersection(self.denied_segments)
        if blocked:
            raise SandboxError(f"path segment(s) {sorted(blocked)} are denied")

        if self.extensions and resolved.suffix:
            if resolved.suffix.lower() not in self.extensions:
                raise SandboxError(
                    f"extension {resolved.suffix!r} is not in the allow-list "
                    f"{list(self.extensions)}"
                )
        return resolved

    # -- operations --------------------------------------------------------
    def read(self, path: str | os.PathLike[str]) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise SandboxError(f"no such file inside sandbox: {path}")
        size = target.stat().st_size
        if size > self.max_bytes:
            raise SandboxError(f"{path} is {size} bytes, over limit {self.max_bytes}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write(self, path: str | os.PathLike[str], content: str) -> Path:
        target = self.resolve(path, for_write=True)
        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            raise SandboxError(
                f"refusing to write {len(data)} bytes, over limit {self.max_bytes}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def list(self, path: str | os.PathLike[str] = ".") -> list[str]:
        target = self.resolve(path)
        if not target.is_dir():
            raise SandboxError(f"{path} is not a directory")
        return sorted(p.name for p in target.iterdir())

    def delete(self, path: str | os.PathLike[str]) -> None:
        target = self.resolve(path, for_write=True)
        if not target.exists():
            raise SandboxError(f"cannot delete missing path {path}")
        if target.is_dir():
            raise SandboxError("refusing to delete a directory from a sandbox")
        target.unlink()

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 30.0,
        cwd: str | os.PathLike[str] = ".",
    ) -> dict[str, Any]:
        """Run a subprocess inside the jail (disabled unless explicitly enabled)."""
        self.require("exec")
        if not self.allow_subprocess:
            raise SandboxError(
                "subprocess execution is disabled; construct the sandbox with "
                "allow_subprocess=True and ring=Ring.TRUSTED"
            )
        if not argv or not isinstance(argv, (list, tuple)):
            raise SandboxError("argv must be a non-empty list of strings")
        workdir = self.resolve(cwd)
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"command timed out after {timeout}s: {argv[0]}") from exc
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout[: self.max_bytes],
            "stderr": completed.stderr[: self.max_bytes],
            "argv": list(argv),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "ring": self.ring.name,
            "permissions": sorted(_PERMISSIONS[self.ring]),
            "max_bytes": self.max_bytes,
            "allow_subprocess": self.allow_subprocess,
            "extensions": list(self.extensions),
            "denied_segments": list(self.denied_segments),
        }


__all__ = ["Ring", "Sandbox"]
