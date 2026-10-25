# rister:ignore-file SEC015,SEC017
"""Built-in security rules.

Each rule is a small, explainable regex check with a CWE reference and a fix
hint. Rules are data, so you can add, disable or reweight them per repository.

These are **heuristics**, not a proof of absence of bugs. They are designed to be
fast, deterministic and dependency-free so they can run on every commit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

SEVERITIES = ("info", "low", "medium", "high", "critical")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}


@dataclass
class Rule:
    """One detectable pattern."""

    id: str
    title: str
    severity: str
    patterns: tuple[str, ...]
    message: str
    cwe: str = ""
    fix: str = ""
    extensions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"rule {self.id}: unknown severity {self.severity!r}")
        self.patterns = tuple(self.patterns)
        self._compiled = tuple(re.compile(p, re.M) for p in self.patterns)

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return self._compiled

    def applies_to(self, suffix: str) -> bool:
        return not self.extensions or suffix.lower() in self.extensions

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "cwe": self.cwe,
            "message": self.message,
            "fix": self.fix,
            "extensions": list(self.extensions),
            "tags": list(self.tags),
            "enabled": self.enabled,
        }


_PY = (".py",)
_JS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")
_CONFIG = (".json", ".yml", ".yaml", ".toml", ".ini", ".env", "")

RULES: tuple[Rule, ...] = (
    Rule(
        "SEC001", "Hardcoded credential", "critical",
        (
            r"(?i)\b(api[_-]?key|secret|password|passwd|token|access[_-]?key)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]",
            r"(?i)\b(aws_secret_access_key|private_key)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]",
        ),
        "A credential appears to be hardcoded in source.",
        cwe="CWE-798",
        fix="Move the value to a secret manager or environment variable and rotate it.",
        tags=("secrets",),
    ),
    Rule(
        "SEC002", "Dangerous dynamic evaluation (Python)", "high",
        (r"\beval\s*\(", r"\bexec\s*\(", r"\b__import__\s*\("),
        "Dynamic evaluation of a string can execute arbitrary code.",
        cwe="CWE-95",
        fix="Replace eval/exec with an explicit dispatch table or a parser.",
        extensions=_PY,
        tags=("injection",),
    ),
    Rule(
        "SEC003", "Command execution with shell=True", "high",
        (r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", r"os\.system\s*\(", r"os\.popen\s*\("),
        "Building a shell command from data enables command injection.",
        cwe="CWE-78",
        fix="Pass an argument list and leave shell=False; validate each argument.",
        extensions=_PY,
        tags=("injection",),
    ),
    Rule(
        "SEC004", "Unsafe YAML load", "high",
        (r"yaml\.load\s*\((?![^)]*SafeLoader)",),
        "yaml.load without SafeLoader can instantiate arbitrary objects.",
        cwe="CWE-502",
        fix="Use yaml.safe_load(...) or pass Loader=yaml.SafeLoader.",
        extensions=_PY,
        tags=("deserialization",),
    ),
    Rule(
        "SEC005", "Unsafe deserialisation", "high",
        (r"\bpickle\.loads?\s*\(", r"\bmarshal\.loads?\s*\(", r"\bjsonpickle\.decode\s*\("),
        "Deserialising untrusted data can lead to remote code execution.",
        cwe="CWE-502",
        fix="Prefer JSON or a signed, versioned schema for untrusted input.",
        extensions=_PY,
        tags=("deserialization",),
    ),
    Rule(
        "SEC006", "SQL built by string formatting", "high",
        (r"(?i)(execute|executemany)\s*\(\s*[f]?['\"].*(select|insert|update|delete).*(\{|\+|%s?['\"])",
         r"(?i)(cursor\.execute)\s*\(\s*['\"].*\+\s*"),
        "String-built SQL allows injection.",
        cwe="CWE-89",
        fix="Use parameterised queries: cursor.execute(sql, params).",
        extensions=_PY + _JS,
        tags=("injection",),
    ),
    Rule(
        "SEC007", "Path traversal risk", "medium",
        (r"(?i)open\s*\(\s*(os\.path\.join\s*\([^)]*request|.*\+.*request\.|f['\"].*\{.*request)",
         r"(?i)(send_file|sendfile|readfile)\s*\(.*(req\.|request\.)"),
        "A filesystem path is built from request data without containment checks.",
        cwe="CWE-22",
        fix="Resolve the path and verify it stays inside an allow-listed root.",
        tags=("injection",),
    ),
    Rule(
        "SEC008", "TLS verification disabled", "high",
        (r"(?i)verify\s*=\s*False", r"(?i)rejectUnauthorized\s*:\s*false",
         r"(?i)NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0", r"(?i)ssl\._create_unverified_context"),
        "Certificate verification is turned off, enabling interception.",
        cwe="CWE-295",
        fix="Remove the override; fix the trust store instead.",
        tags=("crypto",),
    ),
    Rule(
        "SEC009", "Weak hash algorithm", "medium",
        (r"(?i)hashlib\.(md5|sha1)\s*\(", r"(?i)\b(md5|sha1)\s*\(",
         r"(?i)createHash\s*\(\s*['\"](md5|sha1)['\"]"),
        "MD5/SHA-1 are unsuitable for security purposes.",
        cwe="CWE-327",
        fix="Use SHA-256+ for integrity, and a password hash (argon2/scrypt/bcrypt) for secrets.",
        tags=("crypto",),
    ),
    Rule(
        "SEC010", "Insecure randomness for secrets", "medium",
        (r"(?i)(random\.(random|randint|choice)\s*\().*(token|secret|password|nonce|key)",
         r"(?i)(token|secret|password|nonce|key).*(random\.(random|randint|choice)\s*\()"),
        "A non-cryptographic RNG is used where unpredictability matters.",
        cwe="CWE-338",
        fix="Use secrets.token_urlsafe() / os.urandom() (Python) or crypto.randomBytes (Node).",
        extensions=_PY + _JS,
        tags=("crypto",),
    ),
    Rule(
        "SEC011", "Debug mode enabled", "medium",
        (r"(?i)\bdebug\s*=\s*True", r"(?i)DEBUG\s*[:=]\s*['\"]?(true|1)"),
        "Debug mode can leak stack traces and enable consoles.",
        cwe="CWE-489",
        fix="Gate debug behind an explicit local-development setting.",
        tags=("config",),
    ),
    Rule(
        "SEC012", "Wildcard CORS", "medium",
        (r"(?i)allow_origins\s*=\s*\[\s*['\"]\*['\"]", r"(?i)Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]\*",
         r"(?i)cors\s*\(\s*\{\s*origin\s*:\s*['\"]\*['\"]"),
        "Allowing any origin exposes authenticated endpoints to any site.",
        cwe="CWE-942",
        fix="Allow-list trusted origins and send credentials only to them.",
        tags=("config",),
    ),
    Rule(
        "SEC013", "JWT algorithm not verified", "high",
        (r"(?i)algorithms?\s*=\s*\[\s*['\"]none['\"]", r"(?i)verify_signature\s*[:=]\s*(False|false)",
         r"(?i)jwt\.decode\([^)]*options\s*=\s*\{[^}]*verify[^}]*False"),
        "Skipping signature verification lets anyone forge a token.",
        cwe="CWE-347",
        fix="Always verify with an explicit algorithm allow-list; never accept 'none'.",
        tags=("auth",),
    ),
    Rule(
        "SEC014", "Broad file permissions", "medium",
        (r"chmod\s*\(\s*[^,]+,\s*0o?777", r"(?i)mode\s*=\s*0o?777"),
        "World-writable files let other local users modify data or code.",
        cwe="CWE-732",
        fix="Grant the minimum required permissions (for example 0o600 for secrets).",
        extensions=_PY,
        tags=("config",),
    ),
    Rule(
        "SEC015", "Unsanitised HTML rendering", "medium",
        (r"(?i)innerHTML\s*=", r"(?i)dangerouslySetInnerHTML",
         r"(?i)\|\s*safe\b", r"(?i)Markup\s*\("),
        "Rendering untrusted strings as HTML enables cross-site scripting.",
        cwe="CWE-79",
        fix="Render text, or sanitise with a vetted library before injecting HTML.",
        extensions=_JS + _PY,
        tags=("injection",),
    ),
    Rule(
        "SEC016", "Prompt assembled from untrusted input", "medium",
        (r"(?i)(prompt|instructions?)\s*(\+|=|f['\"]).*(user_input|request\.|req\.|input\()",
         r"(?i)f['\"].*(ignore previous|system:|assistant:).*\{"),
        "User input concatenated into a system prompt invites prompt injection.",
        cwe="CWE-1427",
        fix="Keep trusted instructions separate; pass user content as data, never as instructions.",
        tags=("llm",),
    ),
    Rule(
        "SEC017", "Agent tool call without authorisation", "medium",
        (r"(?i)(tool_call|tool_calls|function_call).*(\bexec|\beval|subprocess|os\.system)",
         r"(?i)^(?![ \t]*(?:async[ \t]+)?def[ \t])[^\n]*execute_tool\s*\((?!.*(guard|policy|authoris|authoriz))"),
        "A tool dispatch path appears to run without a policy check.",
        cwe="CWE-862",
        fix="Route every tool invocation through an authorisation guard.",
        tags=("llm", "agents"),
    ),
    Rule(
        "SEC018", "Disabled host key checking (SSH)", "high",
        (r"(?i)StrictHostKeyChecking\s*=\s*no", r"(?i)UserKnownHostsFile\s*=\s*/dev/null"),
        "Disabling host key checks enables man-in-the-middle attacks.",
        cwe="CWE-295",
        fix="Keep StrictHostKeyChecking enabled and manage known_hosts deliberately.",
        tags=("crypto",),
    ),
)

RULES_BY_ID = {rule.id: rule for rule in RULES}


def get_rules(
    only: Sequence[str] | None = None,
    skip: Sequence[str] | None = None,
    min_severity: str = "info",
) -> list[Rule]:
    """Select rules by id or by minimum severity."""
    threshold = _SEVERITY_RANK.get(min_severity, 0)
    selected = []
    for rule in RULES:
        if only and rule.id not in only:
            continue
        if skip and rule.id in skip:
            continue
        if _SEVERITY_RANK[rule.severity] < threshold:
            continue
        selected.append(rule)
    return selected


def severity_at_least(value: str, minimum: str) -> bool:
    return _SEVERITY_RANK.get(value, -1) >= _SEVERITY_RANK.get(minimum, 0)


__all__ = ["SEVERITIES", "Rule", "RULES", "RULES_BY_ID", "get_rules", "severity_at_least"]
