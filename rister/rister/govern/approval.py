"""Human approval for sensitive actions.

A policy can return ``approve`` instead of ``allow``/``deny``. The guard then
asks an approver. Three are provided:

* :class:`AutoApprover`   — a predicate; useful for tests and low-risk tools.
* :class:`QueueApprover`  — records a ticket and blocks until a human decides.
* :class:`CallbackApprover` — delegates to your own function.

Nothing here performs I/O beyond an optional callback, so it is safe to use in
tests and inside a request handler.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.types import new_id

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


@dataclass
class Ticket:
    """A pending request for sign-off."""

    action: str
    reason: str = ""
    subject: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("ticket"))
    status: str = PENDING
    created: float = field(default_factory=time.time)
    decided: float | None = None
    decided_by: str | None = None
    note: str = ""

    @property
    def open(self) -> bool:
        return self.status == PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "reason": self.reason,
            "subject": self.subject,
            "arguments": self.arguments,
            "status": self.status,
            "created": self.created,
            "decided": self.decided,
            "decided_by": self.decided_by,
            "note": self.note,
        }


class QueueApprover:
    """Collect tickets and wait for an explicit decision.

    Use :meth:`approve` / :meth:`deny` from a UI, a CLI command or a graph node
    that pauses on an interrupt.
    """

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout
        self._tickets: dict[str, Ticket] = {}
        self._decided = threading.Condition()
        self._lock = threading.RLock()

    def request(
        self,
        action: str,
        reason: str = "",
        subject: Any = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> Ticket:
        ticket = Ticket(
            action=action,
            reason=reason,
            subject=getattr(subject, "id", subject) if subject is not None else None,
            arguments=dict(arguments or {}),
        )
        with self._lock:
            self._tickets[ticket.id] = ticket
        return ticket

    def pending(self) -> list[Ticket]:
        with self._lock:
            return [t for t in self._tickets.values() if t.open]

    def get(self, ticket_id: str) -> Ticket | None:
        return self._tickets.get(ticket_id)

    def decide(self, ticket_id: str, approved: bool, by: str = "human", note: str = "") -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise KeyError(f"unknown ticket {ticket_id!r}")
            if not ticket.open:
                return ticket
            ticket.status = APPROVED if approved else DENIED
            ticket.decided = time.time()
            ticket.decided_by = by
            ticket.note = note
        with self._decided:
            self._decided.notify_all()
        return ticket

    def approve(self, ticket_id: str, by: str = "human", note: str = "") -> Ticket:
        return self.decide(ticket_id, True, by, note)

    def deny(self, ticket_id: str, by: str = "human", note: str = "") -> Ticket:
        return self.decide(ticket_id, False, by, note)

    def wait(self, ticket_id: str) -> Ticket:
        """Block until the ticket is decided (honours ``timeout``)."""
        deadline = None if self.timeout is None else time.time() + self.timeout
        with self._decided:
            while True:
                ticket = self._tickets.get(ticket_id)
                if ticket is None:
                    raise KeyError(f"unknown ticket {ticket_id!r}")
                if not ticket.open:
                    return ticket
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    return ticket
                self._decided.wait(remaining)

    def all_tickets(self) -> list[Ticket]:
        with self._lock:
            return list(self._tickets.values())


class AutoApprover:
    """Approve automatically according to a predicate."""

    def __init__(self, predicate: Callable[[Ticket], bool] | None = None) -> None:
        self.predicate = predicate or (lambda ticket: True)

    def request(self, action: str, reason: str = "", subject: Any = None,
                arguments: Mapping[str, Any] | None = None) -> Ticket:
        ticket = Ticket(
            action=action,
            reason=reason,
            subject=getattr(subject, "id", subject) if subject is not None else None,
            arguments=dict(arguments or {}),
        )
        approved = bool(self.predicate(ticket))
        ticket.status = APPROVED if approved else DENIED
        ticket.decided = time.time()
        ticket.decided_by = "auto"
        ticket.note = "auto-approved" if approved else "auto-denied"
        return ticket


class CallbackApprover:
    """Ask your own function for a decision."""

    def __init__(self, fn: Callable[[Ticket], bool], by: str = "callback") -> None:
        self.fn = fn
        self.by = by

    def request(self, action: str, reason: str = "", subject: Any = None,
                arguments: Mapping[str, Any] | None = None) -> Ticket:
        ticket = Ticket(
            action=action,
            reason=reason,
            subject=getattr(subject, "id", subject) if subject is not None else None,
            arguments=dict(arguments or {}),
        )
        approved = bool(self.fn(ticket))
        ticket.status = APPROVED if approved else DENIED
        ticket.decided = time.time()
        ticket.decided_by = self.by
        return ticket


__all__ = [
    "PENDING",
    "APPROVED",
    "DENIED",
    "Ticket",
    "QueueApprover",
    "AutoApprover",
    "CallbackApprover",
]
