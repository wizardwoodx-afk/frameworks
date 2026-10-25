"""Consumption budgets: tokens, money, time and call counts.

A policy answers *"may this action happen?"*. A budget answers *"can we still
afford it?"* — the second half of OWASP LLM06 (unbounded consumption) and the
reason runaway agents are expensive.

Prices are supplied by you: RISTER ships no vendor price list, because a stale
price table in a framework is worse than none.

    budget = Budget(max_tokens=50_000, max_cost=0.25, max_requests=40,
                    cost_per_1k_in=0.003, cost_per_1k_out=0.015)
    agent = Agent("a", model=BudgetedModel(model, budget), budget=budget)
    budget.snapshot()      # {'tokens': ..., 'cost': ..., 'requests': ...}
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core.errors import BudgetExceeded, ModelError
from ..core.types import Completion, Usage


@dataclass
class Budget:
    """Cumulative limits for one run, agent or tenant.

    Every field is optional; only the ones you set are enforced. ``charge`` is
    called by :class:`BudgetedModel` and by tools that consume quota, so the
    numbers are never guessed from elsewhere.
    """

    max_tokens: int | None = None
    max_cost: float | None = None
    max_requests: int | None = None
    max_tool_calls: int | None = None
    max_seconds: float | None = None

    # Price per 1,000 tokens. Set both to enable cost accounting.
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0

    name: str = "run"

    # -- live counters (not part of the constructor) -----------------------
    tokens: int = field(default=0, init=False)
    input_tokens: int = field(default=0, init=False)
    output_tokens: int = field(default=0, init=False)
    cost: float = field(default=0.0, init=False)
    requests: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    _started: float = field(default_factory=time.perf_counter, init=False, repr=False)
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _exhausted: str | None = field(default=None, init=False, repr=False)

    # -- accounting --------------------------------------------------------
    def charge(self, usage: Usage | Mapping[str, Any] | None = None,
               *, requests: int = 1) -> "Budget":
        """Record one model call's usage, then enforce every limit."""
        with self._lock:
            if usage is not None:
                prompt = int(getattr(usage, "prompt_tokens", 0) if not isinstance(usage, Mapping)
                             else usage.get("prompt_tokens", 0) or 0)
                completion = int(getattr(usage, "completion_tokens", 0) if not isinstance(usage, Mapping)
                                 else usage.get("completion_tokens", 0) or 0)
                self.input_tokens += prompt
                self.output_tokens += completion
                total = int(getattr(usage, "total_tokens", prompt + completion) if not isinstance(usage, Mapping)
                            else usage.get("total_tokens", prompt + completion) or 0)
                self.tokens += total or (prompt + completion)
            self.cost += (
                (getattr(usage, "prompt_tokens", 0) if not isinstance(usage, Mapping)
                 else usage.get("prompt_tokens", 0) or 0) / 1000.0 * self.cost_per_1k_in
                + (getattr(usage, "completion_tokens", 0) if not isinstance(usage, Mapping)
                   else usage.get("completion_tokens", 0) or 0) / 1000.0 * self.cost_per_1k_out
            )
            self.requests += requests
            self.check()
            return self

    def charge_tool_call(self, count: int = 1) -> "Budget":
        with self._lock:
            self.tool_calls += count
            self.check()
            return self

    def check(self) -> "Budget":
        """Raise :class:`BudgetExceeded` if any limit is crossed."""
        with self._lock:
            if self.max_tokens is not None and self.tokens > self.max_tokens:
                self._fail("token", self.tokens, self.max_tokens, "tokens")
            if self.max_cost is not None and self.cost > self.max_cost:
                self._fail("cost", self.cost, self.max_cost, "")
            if self.max_requests is not None and self.requests > self.max_requests:
                self._fail("request", self.requests, self.max_requests, "requests")
            if self.max_tool_calls is not None and self.tool_calls > self.max_tool_calls:
                self._fail("tool-call", self.tool_calls, self.max_tool_calls, "calls")
            if self.max_seconds is not None:
                elapsed = self.elapsed
                if elapsed > self.max_seconds:
                    self._fail("time", elapsed, self.max_seconds, "s")
        return self

    def _fail(self, limit: str, used: float, allowed: float, unit: str) -> None:
        self._exhausted = limit
        raise BudgetExceeded(limit, used, allowed, unit)

    # -- inspection --------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    @property
    def exhausted(self) -> bool:
        return self._exhausted is not None

    @property
    def remaining_tokens(self) -> int | None:
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.tokens)

    @property
    def remaining_cost(self) -> float | None:
        if self.max_cost is None:
            return None
        return max(0.0, self.max_cost - self.cost)

    def reset(self) -> "Budget":
        with self._lock:
            self.tokens = self.input_tokens = self.output_tokens = 0
            self.cost = 0.0
            self.requests = self.tool_calls = 0
            self._started = time.perf_counter()
            self._exhausted = None
        return self

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tokens": self.tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": round(self.cost, 6),
            "requests": self.requests,
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed, 4),
            "limits": {
                "max_tokens": self.max_tokens,
                "max_cost": self.max_cost,
                "max_requests": self.max_requests,
                "max_tool_calls": self.max_tool_calls,
                "max_seconds": self.max_seconds,
            },
            "exhausted": self._exhausted,
            "utilization": self._utilization(),
        }

    def _utilization(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.max_tokens:
            out["tokens"] = round(self.tokens / self.max_tokens, 4)
        if self.max_cost:
            out["cost"] = round(self.cost / self.max_cost, 4)
        if self.max_requests:
            out["requests"] = round(self.requests / self.max_requests, 4)
        if self.max_tool_calls:
            out["tool_calls"] = round(self.tool_calls / self.max_tool_calls, 4)
        if self.max_seconds:
            out["time"] = round(self.elapsed / self.max_seconds, 4)
        return out

    def summary(self) -> str:
        parts = [f"{self.tokens} tokens", f"{self.requests} requests"]
        if self.cost:
            parts.append(f"cost {self.cost:.4f}")
        if self.exhausted:
            parts.append(f"EXHAUSTED ({self.exhausted})")
        return f"{self.name}: " + ", ".join(parts)

    def __repr__(self) -> str:
        return f"<Budget {self.summary()}>"


class BudgetedModel:
    """Wrap a model so every call is metered and enforced.

    It is deliberately a plain wrapper, not a subclass: any object with
    ``complete`` can be budgeted, including a third-party backend.
    """

    def __init__(self, inner: Any, budget: Budget, *, name: str | None = None) -> None:
        if not hasattr(inner, "complete"):
            raise ModelError("BudgetedModel needs a model with complete()")
        self.inner = inner
        self.budget = budget
        self.name = name or getattr(inner, "name", "budgeted")

    def complete(self, messages: Any, tools: Any = None, **options: Any) -> Completion:
        self.budget.check()  # refuse before spending anything
        completion = self.inner.complete(messages, tools, **options)
        self.budget.charge(getattr(completion, "usage", None))
        return completion

    async def acomplete(self, messages: Any, tools: Any = None, **options: Any) -> Completion:
        self.budget.check()
        result = await self.inner.acomplete(messages, tools, **options)
        self.budget.charge(getattr(result, "usage", None))
        return result

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "budget": self.budget.snapshot()}

    def __repr__(self) -> str:
        return f"<BudgetedModel {self.name} {self.budget.summary()}>"


class BudgetMiddleware:
    """Count tool calls against a budget by sitting in front of a guard.

    ``ToolRegistry.call`` accepts any object with ``check(...)``, so this can be
    composed with a :class:`~rister.govern.guard.Guard` by wrapping it.
    """

    def __init__(self, budget: Budget, inner: Any = None) -> None:
        self.budget = budget
        self.inner = inner

    def check(self, action: str, **kwargs: Any) -> Any:
        # Refuse before counting, so a refused call never inflates the counter:
        # the number in the report is the number that actually ran.
        limit = self.budget.max_tool_calls
        if limit is not None and self.budget.tool_calls >= limit:
            from ..core.errors import BudgetExceeded

            raise BudgetExceeded("tool-call", self.budget.tool_calls + 1, limit, "calls")
        self.budget.charge_tool_call()
        if self.inner is not None and hasattr(self.inner, "check"):
            return self.inner.check(action, **kwargs)
        return None


__all__ = ["Budget", "BudgetedModel", "BudgetMiddleware"]
