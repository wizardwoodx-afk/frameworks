"""Enforce that RISTER's product surface is free of third-party branding.

The tokens below are assembled from fragments on purpose: this file must be able
to scan the whole package — including itself — without matching its own contents.
Attribution for the projects whose ideas informed RISTER lives in
``THIRD-PARTY-NOTICES.md`` at the repository root, not in the framework.
"""
from __future__ import annotations

from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
ROOT = PACKAGE.parent

# Assembled to keep this module clean under its own scan.
FORBIDDEN = [
    "".join(parts)
    for parts in (
        ("lang", "graph"),
        ("mast", "ra"),
        ("bee", "ai"),
        ("volt", "agent"),
        ("agent-", "governance"),
        ("agent_", "governance"),
        ("code", "x"),
        ("open", "ai"),
        ("lang", "chain"),
        ("anthro", "pic"),
        ("micro", "soft"),
        ("semantic", " kernel"),
        ("auto", "gen"),
    )
]

# Directories whose only job is to document provenance/attribution.
ATTRIBUTION_FILES = {"THIRD-PARTY-NOTICES.md"}


#: This auditor necessarily contains the tokens it searches for (and the URL
#: markers), so it is excluded from its own scan.
_SELF = Path(__file__).resolve()

#: The wire-format helper name that used to carry a vendor name.
_LEGACY_NAME = "".join(("open", "ai"))


def _python_sources() -> list[Path]:
    return [path for path in sorted(PACKAGE.rglob("*.py")) if path.resolve() != _SELF]


def test_no_brand_tokens_in_source():
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {token!r}")
    assert not offenders, "brand tokens found:\n" + "\n".join(offenders)


def test_no_vendor_urls_in_source():
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for marker in ("github.com/", "npmjs.com/", "pypi.org/"):
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {marker!r}")
    assert not offenders, "vendor URLs found:\n" + "\n".join(offenders)


def test_public_api_names_are_neutral():
    import rister

    for name in rister.__all__:
        lowered = name.lower()
        for token in FORBIDDEN:
            normalized = token.replace(" ", "_")
            assert normalized not in lowered, f"export {name!r} carries branding"


def test_cli_commands_are_neutral():
    from rister.__main__ import build_parser

    parser = build_parser()
    actions = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ]
    commands: list[str] = []
    for action in actions:
        commands.extend(str(choice) for choice in action.choices)
    joined = " ".join(commands).lower()
    for token in FORBIDDEN:
        assert token not in joined, f"CLI exposes {token!r}"
    assert {"version", "demo", "scan", "policy", "prompt", "deps", "serve"} <= set(commands)


def test_no_vendor_defaults_in_model_backend():
    """The HTTP backend must never default to a third-party endpoint or key name."""
    from rister.agent.backend import HTTPModel

    model = HTTPModel(model="local", base_url="http://localhost:11434/v1")
    assert "localhost" in model.base_url
    assert "RISTER" in HTTPModel.__dataclass_fields__["api_key_env"].default

    from rister.core.errors import ConfigError

    try:
        HTTPModel(model="local")
        raise AssertionError("HTTPModel must require an explicit base_url")
    except ConfigError:
        pass


def test_wire_format_helpers_use_neutral_names():
    from rister.agent.tool import Tool
    from rister.core.types import Message, ToolCall

    assert not hasattr(Message.user("x"), _LEGACY_NAME)
    assert hasattr(Message.user("x"), "as_chat_message")
    assert not hasattr(ToolCall("t"), "openai")
    assert hasattr(ToolCall("t"), "as_function_call")
    assert hasattr(Tool, "as_function_spec")


def test_package_metadata_is_neutral():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for token in FORBIDDEN:
        assert token not in text, f"pyproject.toml mentions {token!r}"
