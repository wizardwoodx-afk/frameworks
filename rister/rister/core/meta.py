"""Package metadata — one source of truth for the version string.

Every default that has to name a version (the HTTP server banner, the OTLP
resource, the MCP ``clientInfo``, ``pyproject.toml``) reads it from here. A
second hardcoded ``"1.1.0"`` is a future inconsistency, so there is exactly one.
"""
from __future__ import annotations

#: Semantic version of this package. ``test_architecture`` and
#: ``scripts/sync_docs.py`` both compare it against the places that repeat it.
__version__ = "1.1.0"

__all__ = ["__version__"]
