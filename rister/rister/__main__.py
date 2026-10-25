"""Command line interface: ``python -m rister <command>`` or ``rister <command>``.

Commands
--------
  version                       print the framework version
  demo                          run an offline end-to-end demonstration
  scan PATH                     scan a repository for security findings
  policy lint FILE              lint a policy document
  policy explain FILE ACTION    explain a decision (add --arg k=v)
  prompt check FILE             audit a system prompt for injection resistance
  deps check PATH               check dependencies for unpinned/typosquat risk
  serve [--port N]              serve a demo agent over HTTP

Exit codes: 0 success, 1 findings/failures found, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from . import __version__


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _out(text: str = "") -> None:
    print(text)


def _kv(pairs: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--arg expects k=v, got {pair!r}")
        key, value = pair.split("=", 1)
        result[key] = value
    return result


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_version(args: argparse.Namespace) -> int:
    _out(f"rister {__version__}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from .security import Scanner

    scanner = Scanner(min_severity=args.min_severity)
    report = scanner.scan(args.path)
    if args.out:
        target = report.save(args.out, fmt=args.format)
        _out(f"wrote {target}")
    else:
        payload = {
            "json": report.to_json,
            "sarif": report.to_sarif_json,
            "md": report.to_markdown,
            "csv": report.to_csv,
        }[args.format]()
        _out(payload)
    if not args.out:
        print(report.summary(), file=sys.stderr)
    if args.fail_on_severity and report.has_at_or_above(args.fail_on_severity):
        return 1
    return 0


def cmd_policy_lint(args: argparse.Namespace) -> int:
    from .govern import lint_policy_data, load_document

    data = load_document(args.file)
    problems = lint_policy_data(data)
    if not problems:
        _out(f"{args.file}: no problems found")
        return 0
    for problem in problems:
        _out(f"{args.file}: {problem}")
    return 1


def cmd_policy_explain(args: argparse.Namespace) -> int:
    from .govern import Policy

    policy = Policy.load(args.file)
    decision = policy.evaluate(args.action, _kv(args.arg))
    _out(json.dumps(decision.to_dict(), indent=2, sort_keys=True))
    return 0 if decision.allowed else 1


def cmd_prompt_check(args: argparse.Namespace) -> int:
    from .govern import PromptDefense

    defense = PromptDefense(min_grade=args.min_grade)
    report = defense.evaluate_file(args.file)
    if args.json:
        _out(report.to_json())
    else:
        _out(f"{args.file}: {report.summary()}")
    return 1 if report.is_blocking(args.min_grade) else 0


def cmd_deps_check(args: argparse.Namespace) -> int:
    from .govern import SupplyChainGuard

    findings = SupplyChainGuard().scan_directory(args.path)
    if not findings:
        _out(f"{args.path}: no supply-chain problems found")
        return 0
    for finding in findings:
        _out(str(finding))
    return 1


def cmd_redteam(args: argparse.Namespace) -> int:
    """Run the adversarial corpus against a demo agent you can configure."""
    from .agent import Agent, ScriptedModel
    from .evals import red_team
    from .govern import AutoApprover, AuditLog, Guard, Policy

    if args.prompt:
        target = lambda prompt: prompt  # noqa: E731 - identity target for check-only runs
    else:
        policy = Policy.from_dict(
            {
                "name": "redteam",
                "default": "deny",
                "rules": [{"name": "read", "action": ["search_notes", "lookup"], "effect": "allow"}],
            }
        )
        target = Agent(
            "target",
            model=ScriptedModel([args.reply]),
            guard=Guard(policy=policy, audit=AuditLog(), approver=AutoApprover()),
        )

    report = red_team(target, name="cli", canaries=args.canary or ())
    if args.format == "json":
        _out(report.to_json())
    elif args.format == "md":
        _out(report.to_markdown())
    else:
        _out(report.summary())
        for verdict in report.failures():
            _out(f"  FAIL {verdict.attack.id} ({verdict.attack.asi}) — {verdict.reason}")
    if args.fail_under is not None and report.pass_rate < args.fail_under:
        return 1
    return 0 if report.ok else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    """Inspect an MCP server: profile, capabilities and tools."""
    from .mcp import McpClient

    if not args.command and not args.url:
        _out("usage: rister mcp --command <server command...>  |  rister mcp --url http://host/mcp")
        return 2
    try:
        if args.url:
            client = McpClient.http(args.url, timeout=args.timeout)
        else:
            client = McpClient.stdio(args.command, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - surface the reason, do not traceback
        _out(f"connection failed: {exc}")
        return 2
    try:
        tools = client.list_tools()
        if args.format == "json":
            _out(json.dumps({"session": client.describe(), "tools": tools}, indent=2))
        else:
            _out(f"server: {client.server_info.get('name', '?')} "
                 f"({client.server_info.get('version', '?')})")
            _out(f"profile: {client.profile} · protocol {client.protocol_version}")
            _out(f"capabilities: {', '.join(sorted(client.server_capabilities)) or 'none'}")
            _out(f"tools ({len(tools)}):")
            for entry in tools:
                required = ", ".join(entry["inputSchema"]["required"]) or "-"
                _out(f"  {entry['name']:<24} required: {required}")
                if entry["description"]:
                    _out(f"    {entry['description'][:100]}")
    finally:
        client.close()
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(verbose=not args.quiet)


def cmd_serve(args: argparse.Namespace) -> int:
    from .demo import build_demo_app

    app = build_demo_app()
    _out(f"serving RISTER demo on http://{args.host}:{args.port}")
    _out("  GET  /health   GET /describe   POST /agents/assistant   POST /graphs/pipeline")
    app.serve(host=args.host, port=args.port)
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rister",
        description="RISTER — unified agent orchestration, governance and security.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"rister {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("version", help="print the version").set_defaults(func=cmd_version)

    redteam = subs.add_parser("redteam", help="run the adversarial corpus (OWASP ASI01-10)")
    redteam.add_argument("--format", choices=["text", "json", "md"], default="text")
    redteam.add_argument("--reply", default="I can't help with that.",
                         help="scripted reply for the built-in target agent")
    redteam.add_argument("--prompt", action="store_true",
                         help="use an identity target instead of an agent")
    redteam.add_argument("--canary", action="append",
                         help="canary string that must never appear in output")
    redteam.add_argument("--fail-under", type=float,
                         help="exit 1 when the pass rate is below this fraction")
    redteam.set_defaults(func=cmd_redteam)

    mcp = subs.add_parser("mcp", help="inspect an MCP server's tools")
    mcp.add_argument("--command", nargs=argparse.REMAINDER,
                     help="server command, e.g. -- python -m rister.mcp")
    mcp.add_argument("--url", help="Streamable HTTP endpoint instead of a command")
    mcp.add_argument("--timeout", type=float, default=20.0)
    mcp.add_argument("--format", choices=["text", "json"], default="text")
    mcp.set_defaults(func=cmd_mcp)

    demo = subs.add_parser("demo", help="run an offline end-to-end demonstration")
    demo.add_argument("--quiet", action="store_true", help="only print the summary")
    demo.set_defaults(func=cmd_demo)

    scan = subs.add_parser("scan", help="scan a repository for security findings")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--format", choices=["json", "sarif", "md", "csv"], default="md")
    scan.add_argument("--out", help="write the report to this file")
    scan.add_argument("--min-severity", default="info",
                      choices=["info", "low", "medium", "high", "critical"])
    scan.add_argument("--fail-on-severity", choices=["low", "medium", "high", "critical"],
                      help="exit 1 when findings at or above this severity exist")
    scan.set_defaults(func=cmd_scan)

    policy = subs.add_parser("policy", help="lint or explain a policy document")
    policy_subs = policy.add_subparsers(dest="policy_command", required=True)
    lint = policy_subs.add_parser("lint", help="lint a policy file")
    lint.add_argument("file")
    lint.set_defaults(func=cmd_policy_lint)
    explain = policy_subs.add_parser("explain", help="explain a decision for one action")
    explain.add_argument("file")
    explain.add_argument("action")
    explain.add_argument("--arg", action="append", metavar="K=V",
                         help="provide an argument value (repeatable)")
    explain.set_defaults(func=cmd_policy_explain)

    prompt = subs.add_parser("prompt", help="audit system prompts")
    prompt_subs = prompt.add_subparsers(dest="prompt_command", required=True)
    check = prompt_subs.add_parser("check", help="score a system prompt file")
    check.add_argument("file")
    check.add_argument("--min-grade", default="C", choices=list("ABCDEF"))
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_prompt_check)

    deps = subs.add_parser("deps", help="dependency hygiene")
    deps_subs = deps.add_subparsers(dest="deps_command", required=True)
    deps_check = deps_subs.add_parser("check", help="scan a tree for dependency risks")
    deps_check.add_argument("path", nargs="?", default=".")
    deps_check.set_defaults(func=cmd_deps_check)

    serve = subs.add_parser("serve", help="serve the demo app over HTTP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    except FileNotFoundError as exc:
        _out(f"error: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not traceback at users
        from .core.errors import RisterError

        if isinstance(exc, RisterError):
            _out(f"error: {exc}")
            return 2
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
