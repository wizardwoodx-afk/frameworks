"""Scanner, suppressions, tracing, evaluation, HTTP surface and the CLI.

This module deliberately embeds vulnerable code as *test fixtures* — that is what the
scanner is asked to detect — so the scanner's own suppressions are used, scoped to the
exact rules the fixtures trigger, rather than weakening the rules themselves.
# rister:ignore-file SEC001,SEC002,SEC003,SEC004,SEC005,SEC009
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request

from rister.agent import Agent, ScriptedModel
from rister.evals import Contains, Exact, JsonKeys, NumericClose, Suite, evaluate
from rister.observe import CallableExporter, Tracer
from rister.security import Scanner, draft_security_policy, get_rules, scan
from rister.serve import App

VULNERABLE = '''import subprocess, hashlib, pickle, yaml
API_KEY = "sk-live-abcdef1234567890"
def run(cmd):
    subprocess.run(cmd, shell=True)
    eval(cmd)
    pickle.loads(cmd)
    yaml.load(cmd)
    return hashlib.md5(cmd.encode()).hexdigest()
'''

CLEAN = '''def add(a: int, b: int) -> int:
    return a + b
'''


# -- scanner ---------------------------------------------------------------
def test_scanner_finds_expected_rules():
    findings = Scanner().scan_text(VULNERABLE, "bad.py")
    rules = {finding.rule for finding in findings}
    assert {"SEC001", "SEC002", "SEC003", "SEC004", "SEC005", "SEC009"} <= rules
    assert any(f.severity == "critical" for f in findings)


def test_scanner_clean_file_has_no_findings():
    assert Scanner().scan_text(CLEAN, "clean.py") == []


def test_scanner_directory_report_and_formats():
    with tempfile.TemporaryDirectory() as directory:
        os.makedirs(os.path.join(directory, "src"))
        with open(os.path.join(directory, "src", "bad.py"), "w") as handle:
            handle.write(VULNERABLE)
        with open(os.path.join(directory, "src", "clean.py"), "w") as handle:
            handle.write(CLEAN)
        report = scan(directory)
        assert report.files_scanned == 2
        assert report.has_at_or_above("critical")
        assert report.for_path(os.path.join("src", "clean.py")) == []

        sarif = report.to_sarif()
        assert sarif["version"] == "2.1.0"
        assert sarif["runs"][0]["results"]
        assert json.loads(report.to_json())["tool"] == "rister"
        assert report.to_markdown().startswith("# Security scan report")
        assert "severity,rule,title,path,line" in report.to_csv()
        for name in ("r.json", "r.sarif", "r.md", "r.csv"):
            assert report.save(os.path.join(directory, name)).exists()


def test_scanner_suppressions():
    text = (
        'API_KEY = "sk-live-abcdef1234567890"\n'
        "eval(x)  # rister:ignore SEC002\n"
        "# rister:ignore SEC002\n"
        "exec(y)\n"
        "eval(z)\n"
    )
    findings = Scanner().scan_text(text, "s.py")
    lines = {(f.rule, f.line) for f in findings}
    assert ("SEC001", 1) in lines
    assert not any(rule == "SEC002" and line in (2, 4) for rule, line in lines)
    assert ("SEC002", 5) in lines


def test_suppressions_are_recorded_not_hidden():
    """A silenced finding must stay visible in the report: it was a decision."""
    with tempfile.TemporaryDirectory() as directory:
        with open(os.path.join(directory, "s.py"), "w") as handle:
            # a directive covers its own line *and the next one*, so keep a blank
            # line between the suppressed call and the one that must still report
            handle.write("eval(a)  # rister:ignore SEC002\n\nexec(b)\n")
        report = scan(directory)
        assert report.suppressed == 1
        assert [item.rule for item in report.suppressions] == ["SEC002"]
        assert report.suppressions[0].line == 1
        assert report.suppressions[0].directive == "line"
        assert report.suppressions[0].to_dict()["severity"] == "high"
        assert json.loads(report.to_json())["suppressions"][0]["rule"] == "SEC002"
        # the unsuppressed line is still reported
        assert [f.rule for f in report.findings] == ["SEC002"]
        assert report.findings[0].line == 3


def test_rule_filtering_by_severity():
    high = get_rules(min_severity="high")
    everything = get_rules()
    assert len(high) < len(everything)
    assert all(rule.severity in ("high", "critical") for rule in high)


def test_security_policy_draft():
    report = Scanner().scan_text(VULNERABLE, "bad.py")
    draft = draft_security_policy(report, contact="team@example.test")
    assert "SEC001" in draft and "team@example.test" in draft


# -- tracing ---------------------------------------------------------------
def test_tracer_nesting_and_error_capture():
    captured = []
    tracer = Tracer("t", exporters=[CallableExporter(captured.append)])
    with tracer.span("root"):
        with tracer.span("child") as span:
            span.set("k", 1)
        try:
            with tracer.span("boom"):
                raise ValueError("bad")
        except ValueError:
            pass
    assert len(tracer.spans) == 3
    assert tracer.summary()["errors"] == 1
    tracer.export()
    assert captured and captured[0]["summary"]["spans"] == 3


def test_interrupt_is_not_an_error():
    from rister.core.errors import Interrupt
    from rister.graph import END, START, Graph
    from rister.state import State

    class S(State):
        done: bool = False

    tracer = Tracer("t")

    def pause(state, ctx):
        ctx.interrupt({"ask": True})

    graph = Graph(S, "g")
    graph.add_node("pause", pause)
    graph.add_edge(START, "pause")
    graph.add_edge("pause", END)
    graph.compile(tracer=tracer).invoke({})
    assert tracer.summary()["errors"] == 0
    assert any(span.status == "interrupted" for span in tracer.spans)


def test_tool_calls_are_traced():
    from rister.agent import tool

    @tool
    def lookup(query: str) -> str:
        """Look something up."""
        return f"result for {query}"

    tracer = Tracer("t")
    agent = Agent(
        "a",
        model=ScriptedModel([[rs_tool_call("lookup", {"query": "x"})], "done"]),
        tools=[lookup],
        tracer=tracer,
    )
    agent.run("go")
    names = [span.name for span in tracer.spans]
    assert "tool:lookup" in names
    tool_span = next(span for span in tracer.spans if span.name == "tool:lookup")
    assert tool_span.attributes["ok"] is True
    # the tool span nests inside the agent span
    agent_span = next(span for span in tracer.spans if span.name == "agent:a")
    assert tool_span.parent_id == agent_span.id
    assert tracer.summary()["errors"] == 0


def rs_tool_call(name, arguments):
    from rister.core.types import ToolCall

    return ToolCall(name, arguments)


# -- evals -----------------------------------------------------------------
def test_suite_scoring_and_report():
    suite = Suite(
        "math",
        [{"input": "2+2", "expected": "4", "name": "simple"},
         {"input": "capital", "expected": "Paris", "name": "capital"}],
        [Exact()],
    )
    report = suite.run(lambda question: "4" if question == "2+2" else "Lyon")
    assert report.total == 2 and report.passed == 1 and report.failed == 1
    assert report.pass_rate == 0.5
    assert report.to_markdown().startswith("# Eval report")
    assert json.loads(report.to_json())["passed"] == 1


def test_scorers():
    assert Exact()("hello", {"expected": "HELLO"}).passed
    assert Contains()("the capital is Paris", {"expected": ["paris"]}).passed
    assert NumericClose()("answer 42", {"expected": 42}).passed
    assert JsonKeys()('{"name": "Ada", "age": 36}',
                      {"expected": {"name": "string", "age": "integer"}}).passed
    assert not JsonKeys()('{"name": "Ada"}',
                          {"expected": {"name": "string", "age": "integer"}}).passed


def test_evaluate_agent_target():
    agent = Agent("bot", model=ScriptedModel(["The answer is 42"]))
    report = evaluate([{"input": "q", "expected": 42}], [NumericClose()], agent, name="agent")
    assert report.passed == 1


def test_suite_requires_scorer():
    from rister.core.errors import EvalError

    try:
        Suite("empty", [], [])
        raise AssertionError("expected EvalError")
    except EvalError:
        pass


# -- serving ---------------------------------------------------------------
def test_app_routes_without_sockets():
    agent = Agent("echo", model=ScriptedModel(["hi there"]))
    app = App(agents={"echo": agent})
    status, body = app.handle("GET", "/health")
    assert status == 200 and body["ok"]
    status, body = app.handle("GET", "/describe")
    assert "agents" in body and "echo" in body["agents"]
    status, body = app.handle("POST", "/agents/echo", {"input": "hello"})
    assert status == 200 and body["text"] == "hi there"
    assert app.handle("POST", "/agents/missing", {"input": "x"})[0] == 404
    assert app.handle("POST", "/agents/echo", {})[0] == 400
    assert app.handle("GET", "/nope")[0] == 404


def test_app_auth_token():
    app = App(agents={}, auth_token="s3cret")
    assert app.handle("GET", "/health")[0] == 401
    assert app.handle("GET", "/health", headers={"Authorization": "Bearer s3cret"})[0] == 200


def test_app_over_real_http():
    agent = Agent("echo", model=ScriptedModel(["pong"]))
    app = App(agents={"echo": agent})
    server = app.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
            assert json.loads(response.read())["ok"] is True
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/agents/echo",
            data=json.dumps({"input": "ping"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.loads(response.read())["text"] == "pong"
    finally:
        server.shutdown()
        server.server_close()


# -- cli -------------------------------------------------------------------
def test_cli_version_and_scan(tmp_path=None):
    from rister.__main__ import main

    assert main(["version"]) == 0
    with tempfile.TemporaryDirectory() as directory:
        with open(os.path.join(directory, "bad.py"), "w") as handle:
            handle.write(VULNERABLE)
        out = os.path.join(directory, "report.json")
        assert main(["scan", directory, "--format", "json", "--out", out]) == 0
        assert json.loads(open(out).read())["findings"]
        assert main(["scan", directory, "--fail-on-severity", "critical", "--out", out]) == 1


def test_cli_policy_and_prompt_commands():
    from rister.__main__ import main
    from rister.govern import Policy

    with tempfile.TemporaryDirectory() as directory:
        policy_path = str(Policy.from_dict(
            {"name": "t", "default": "deny",
             "rules": [{"name": "r", "action": "read_*", "effect": "allow"}]}
        ).save(os.path.join(directory, "p.json")))
        assert main(["policy", "lint", policy_path]) == 0
        assert main(["policy", "explain", policy_path, "read_file"]) == 0
        assert main(["policy", "explain", policy_path, "delete_x"]) == 1

        prompt_path = os.path.join(directory, "sys.txt")
        open(prompt_path, "w").write("You are helpful.")
        assert main(["prompt", "check", prompt_path]) == 1  # grades F


def test_demo_runs_offline():
    from rister.demo import run_demo

    assert run_demo(verbose=False) == 0
