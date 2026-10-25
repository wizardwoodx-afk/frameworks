"""Typed shared state with pluggable reducers.

A :class:`State` subclass declares *fields*; each field is bound to a
:class:`Reducer` that decides how an update merges with the current value. This
is the whole trick behind deterministic orchestration: nodes never mutate the
state object, they return a partial update, and the reducers define the algebra.

    class Draft(State):
        topic: str = ""                 # Last (replace)
        notes: list = Append()          # append, preserving order
        scores: dict = Merge()          # shallow dict merge
        total: int = Sum()              # numeric accumulation
        tags: list = Unique()           # append without duplicates
        best: int = Max()

Reducers are declared as class attributes and removed from the class namespace
on subclass creation, so ``instance.field`` always resolves through the runtime
value, never through the descriptor.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Iterator, Mapping

from .core.errors import StateError

_MISSING = object()


# --------------------------------------------------------------------------
# reducers
# --------------------------------------------------------------------------
class Reducer:
    """Combines a current value with an incoming update."""

    kind = "last"

    def __init__(self, default: Any = None) -> None:
        self.default = default

    # -- lifecycle ---------------------------------------------------------
    def _empty(self) -> Any:
        return None

    def initial(self) -> Any:
        if self.default is not None:
            return copy.deepcopy(self.default)
        return self._empty()

    def coerce(self, value: Any) -> Any:
        """Normalise a value supplied as an initial state."""
        return value

    # -- merge -------------------------------------------------------------
    def reduce(self, current: Any, value: Any) -> Any:  # noqa: D401 - trivial
        """Return the merged value. Replacing semantics by default."""
        return value

    def describe(self) -> dict[str, Any]:
        return {"reducer": self.kind, "default": self.default}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.default!r})"


class Last(Reducer):
    """Replace the previous value (the default)."""

    kind = "last"


class Append(Reducer):
    """Extend a list, preserving order. A scalar is appended as one item."""

    kind = "append"

    def _empty(self) -> list:
        return []

    def coerce(self, value: Any) -> list:
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    def reduce(self, current: Any, value: Any) -> list:
        out = list(current or [])
        items = value if isinstance(value, (list, tuple)) else [value]
        return out + list(items)


class Unique(Reducer):
    """Append items that are not already present (order preserving)."""

    kind = "unique"

    def _empty(self) -> list:
        return []

    def coerce(self, value: Any) -> list:
        return list(value) if isinstance(value, (list, tuple)) else [value]

    def reduce(self, current: Any, value: Any) -> list:
        out = list(current or [])
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if item not in out:
                out.append(item)
        return out


class Merge(Reducer):
    """Shallow-merge mappings."""

    kind = "merge"

    def _empty(self) -> dict:
        return {}

    def coerce(self, value: Any) -> dict:
        if not isinstance(value, Mapping):
            raise StateError(f"Merge reducer expects a mapping, got {type(value).__name__}")
        return dict(value)

    def reduce(self, current: Any, value: Any) -> dict:
        if not isinstance(value, Mapping):
            raise StateError(f"Merge reducer expects a mapping, got {type(value).__name__}")
        out = dict(current or {})
        out.update(value)
        return out


class Sum(Reducer):
    """Add numbers (useful for counters and token totals)."""

    kind = "sum"

    def _empty(self) -> int:
        return 0

    def reduce(self, current: Any, value: Any) -> Any:
        return (current or 0) + value


class Max(Reducer):
    """Keep the larger value."""

    kind = "max"

    def reduce(self, current: Any, value: Any) -> Any:
        if current is None:
            return value
        return max(current, value)


class Min(Reducer):
    """Keep the smaller value."""

    kind = "min"

    def reduce(self, current: Any, value: Any) -> Any:
        if current is None:
            return value
        return min(current, value)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
class State:
    """A typed bag of values merged through reducers.

    Subclasses declare fields with annotations; a reducer instance used as the
    class attribute selects the merge behaviour, and any plain value becomes the
    default for a :class:`Last` reducer.
    """

    __fields__: dict[str, Reducer] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Start from the already-resolved fields of every base (nearest base wins,
        # so iterate from the root of the MRO downwards).
        fields: dict[str, Reducer] = {}
        for base in reversed(cls.__mro__[1:]):
            inherited = base.__dict__.get("__fields__")
            if isinstance(inherited, dict):
                fields.update(inherited)

        # Then apply this class's own annotations. Reducer descriptors are read
        # once and removed from the namespace, so attribute access at runtime
        # always resolves to the merged value.
        for name in cls.__dict__.get("__annotations__", {}):
            if name.startswith("_"):
                continue
            raw = cls.__dict__.get(name, _MISSING)
            if isinstance(raw, Reducer):
                fields[name] = raw
            else:
                fields[name] = Last(None if raw is _MISSING else raw)
            # Remove the class-level definition either way: a reducer descriptor
            # or a plain default would shadow the merged runtime value on the
            # instance and silently make reads return the default.
            if name in cls.__dict__:
                delattr(cls, name)

        cls.__fields__ = fields

    # -- construction ------------------------------------------------------
    #: Reject unknown field names. A State with no declared fields (the bare
    #: base class) is treated as a dynamic bag and stays permissive.
    __strict__ = True

    def __init__(self, **initial: Any) -> None:
        object.__setattr__(self, "_data", {})
        data: dict[str, Any] = self._data
        for name, reducer in self.__fields__.items():
            data[name] = reducer.initial()
        strict = self.__strict__ and bool(self.__fields__)
        for key, value in initial.items():
            reducer = self.__fields__.get(key)
            if reducer is None:
                if strict:
                    raise StateError(
                        f"{type(self).__name__} has no field {key!r}; "
                        f"valid fields: {sorted(self.__fields__)}"
                    )
                data[key] = value
            else:
                data[key] = reducer.coerce(value)

    # -- attribute access --------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_data"][name]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__!s} has no field {name!r}"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._data[name] = value

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self) -> Iterable[str]:
        return self._data.keys()

    def items(self) -> Iterable[tuple[str, Any]]:
        return self._data.items()

    # -- merging -----------------------------------------------------------
    def update(self, delta: Mapping[str, Any] | None) -> "State":
        """Return a **new** state with ``delta`` merged through the reducers."""
        new = self.copy()
        if not delta:
            return new
        if not isinstance(delta, Mapping):
            raise StateError(
                f"state update must be a mapping, got {type(delta).__name__}"
            )
        strict = self.__strict__ and bool(self.__fields__)
        for key, value in delta.items():
            reducer = self.__fields__.get(key)
            if reducer is None:
                if strict:
                    raise StateError(
                        f"{type(self).__name__} has no field {key!r}; "
                        f"a node update may only set {sorted(self.__fields__)}"
                    )
                new._data[key] = value
            else:
                try:
                    new._data[key] = reducer.reduce(new._data.get(key), value)
                except Exception as exc:  # noqa: BLE001 - re-raise with context
                    raise StateError(
                        f"reducer for field {key!r} rejected update: {exc}"
                    ) from exc
        return new

    def copy(self) -> "State":
        clone = type(self)()
        clone._data = copy.deepcopy(self._data)
        return clone

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "State":
        return cls(**dict(data))

    @classmethod
    def fields(cls) -> dict[str, Reducer]:
        return dict(cls.__fields__)

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return {name: reducer.describe() for name, reducer in cls.__fields__.items()}

    # -- dunder ------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if isinstance(other, State):
            return self.to_dict() == other.to_dict()
        if isinstance(other, Mapping):
            return self.to_dict() == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        body = ", ".join(f"{k}={v!r}" for k, v in self._data.items())
        return f"{type(self).__name__}({body})"


__all__ = [
    "Reducer",
    "Last",
    "Append",
    "Unique",
    "Merge",
    "Sum",
    "Max",
    "Min",
    "State",
]
