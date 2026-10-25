"""Agent identity and signed capability tokens.

In a multi-agent system an API key identifies *a deployment*, not *an agent*.
RISTER gives every actor a stable id, a set of roles and free-form attributes,
and can mint short-lived HMAC-signed tokens that travel with a task so a
delegation can be attributed and bounded.

Uses only the standard library (``hmac``/``hashlib``/``secrets``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..core.errors import PolicyError

KIND_AGENT = "agent"
KIND_HUMAN = "human"
KIND_SERVICE = "service"
KIND_SYSTEM = "system"


@dataclass(frozen=True)
class Identity:
    """Who is acting."""

    id: str
    kind: str = KIND_AGENT
    roles: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise PolicyError("identity needs a non-empty id")

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "roles": list(self.roles),
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Identity":
        return cls(
            id=str(data["id"]),
            kind=data.get("kind", KIND_AGENT),
            roles=tuple(data.get("roles") or ()),
            attributes=dict(data.get("attributes") or {}),
        )

    def __str__(self) -> str:
        return self.id


SYSTEM = Identity(id="system", kind=KIND_SYSTEM, roles=("admin",))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class Signer:
    """Mint and verify HMAC-SHA256 capability tokens."""

    def __init__(
        self,
        key: str | bytes | None = None,
        *,
        key_env: str = "RISTER_KEY",
        default_ttl: int = 3600,
    ) -> None:
        material = key or os.environ.get(key_env) or secrets.token_hex(32)
        self._key = material.encode("utf-8") if isinstance(material, str) else material
        self.default_ttl = default_ttl

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).digest())

    def issue(
        self,
        identity: Identity,
        *,
        ttl: int | None = None,
        scopes: Iterable[str] = (),
        audience: str | None = None,
    ) -> str:
        """Return ``base64(payload).signature`` for an identity."""
        issued = int(time.time())
        payload = {
            "sub": identity.id,
            "kind": identity.kind,
            "roles": list(identity.roles),
            "attributes": dict(identity.attributes),
            "scopes": list(scopes),
            "aud": audience,
            "iat": issued,
            "exp": issued + (self.default_ttl if ttl is None else ttl),
        }
        body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, *, audience: str | None = None) -> dict[str, Any]:
        """Validate a token and return its payload. Raises :class:`PolicyError`."""
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise PolicyError("malformed token") from exc
        if not hmac.compare_digest(self._sign(body), signature):
            raise PolicyError("token signature is invalid")
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise PolicyError("token payload is unreadable") from exc
        if int(payload.get("exp", 0)) < int(time.time()):
            raise PolicyError("token has expired")
        if audience is not None and payload.get("aud") not in (None, audience):
            raise PolicyError(f"token audience {payload.get('aud')!r} != {audience!r}")
        return payload

    def identity(self, token: str, **kwargs: Any) -> Identity:
        """Verify a token and reconstruct the acting :class:`Identity`."""
        payload = self.verify(token, **kwargs)
        return Identity(
            id=str(payload["sub"]),
            kind=payload.get("kind", KIND_AGENT),
            roles=tuple(payload.get("roles") or ()),
            attributes=dict(payload.get("attributes") or {}),
        )


class Directory:
    """A small in-memory registry of known actors."""

    def __init__(self, identities: Iterable[Identity] = ()) -> None:
        self._by_id: dict[str, Identity] = {}
        for identity in identities:
            self.register(identity)

    def register(self, identity: Identity) -> Identity:
        self._by_id[identity.id] = identity
        return identity

    def get(self, identity_id: str) -> Identity | None:
        return self._by_id.get(identity_id)

    def require(self, identity_id: str) -> Identity:
        found = self._by_id.get(identity_id)
        if found is None:
            raise PolicyError(f"unknown identity {identity_id!r}")
        return found

    def with_role(self, role: str) -> list[Identity]:
        return [i for i in self._by_id.values() if role in i.roles]

    def __len__(self) -> int:
        return len(self._by_id)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [i.to_dict() for i in self._by_id.values()]


__all__ = [
    "KIND_AGENT",
    "KIND_HUMAN",
    "KIND_SERVICE",
    "KIND_SYSTEM",
    "Identity",
    "SYSTEM",
    "Signer",
    "Directory",
]
