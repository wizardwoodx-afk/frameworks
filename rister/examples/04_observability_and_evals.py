"""04 — Observability, evaluation and a live HTTP endpoint.

Run:  python examples/04_observability_and_evals.py
"""
from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from pathlib import Path

import rister as rs


class Work(rs.State):
    question: str = ""
    steps: list = rs.Append()
    answer: str = ""


@rs.tool
def lookup(query: str) -> str:
    """Look up a fact in the local table."""
    return {"pricing": "pricing is 42 units"}.get(query, "unknown")


def build_agent(tracer: rs.Tracer) -> rs.Agent:
    return rs.Agent(
        "assistant",
        model=rs.ScriptedModel([[rs.ToolCall("lookup", {"query": "pricing"})], "42 units"]),
        tools=[lookup],
        tracer=tracer,
    )


with tempfile.TemporaryDirectory() as tmp:
    # ---- tracing ---------------------------------------------------------
    trace_file = Path(tmp) / "traces.jsonl"
    tracer = rs.Tracer("example", exporters=[rs.JSONLExporter(trace_file)])

    agent = build_agent(tracer)

    def ask(state: Work, ctx) -> dict:
        return {"answer": agent.run(state.question).text, "steps": ["asked"]}

    def summarise(state: Work) -> dict:
        return {"steps": ["summarised"]}

    graph = rs.Graph(Work, "work")
    graph.add_node("ask", ask)
    graph.add_node("summarise", summarise)
    graph.add_edge(rs.START, "ask")
    graph.add_edge("ask", "summarise")
    graph.add_edge("summarise", rs.END)
    compiled = graph.compile(tracer=tracer)

    events = list(compiled.stream({"question": "what is the pricing?"}))
    print("1. streamed graph events")
    print(f"   {[event['event'] for event in events]}")

    print("2. one trace spans the graph, the agent, the model and the tool")
    by_id = {span.id: span for span in tracer.spans}

    def depth_of(span: rs.Span) -> int:
        level, parent = 0, by_id.get(span.parent_id or "")
        while parent is not None:
            level, parent = level + 1, by_id.get(parent.parent_id or "")
        return level

    for span in tracer.spans:
        indent = "  " * depth_of(span)
        print(f"   {indent}{span.name:<18} {span.status:<6} {span.duration_ms:>6.2f}ms")
    summary = tracer.summary()
    print(f"   summary: {summary['spans']} spans, {summary['errors']} errors")

    tracer.export()
    print(f"3. exported to JSONL: {len(trace_file.read_text().strip().splitlines())} line(s)")

    # ---- evaluation ------------------------------------------------------
    # Every scorer in a suite is applied to every case, so group cases by what
    # they are meant to prove instead of mixing incompatible expectations.
    suites = [
        rs.Suite(
            "answers-the-question",
            [
                {"name": "mentions-units", "input": "what is the pricing?",
                 "expected": ["42 units"]},
                {"name": "cites-figure", "input": "what is the pricing?",
                 "expected": ["42"]},
            ],
            [rs.Contains()],
        ),
        rs.Suite(
            "numeric-agreement",
            [{"name": "exact-number", "input": "what is the pricing?", "expected": 42}],
            [rs.NumericClose()],
        ),
    ]
    print("4. evaluation")
    for suite in suites:
        suite_report = suite.run(build_agent(tracer))
        print(f"   {suite.name}: {suite_report.summary()}")
        for case in suite_report.results:
            marks = " ".join(
                f"{score.scorer}={'pass' if score.passed else 'fail'}"
                for score in case.scores
            )
            print(f"     {case.case.name:<16} {marks}")
    report = suites[0].run(build_agent(tracer))

    print("5. the report is a file you can diff")
    print("   " + report.to_markdown().splitlines()[0])
    print(f"   json keys: {sorted(json.loads(report.to_json()))}")
    assert report.pass_rate == 1.0, report.to_markdown()

    # ---- serving ---------------------------------------------------------
    app = rs.App(agents={"assistant": agent}, graphs={"work": compiled}, tracer=tracer)
    server = app.make_server(host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            print("6. the app answers over HTTP")
            print(f"   GET /health -> {json.loads(response.read())}")
        with urllib.request.urlopen(f"{base}/describe", timeout=5) as response:
            described = json.loads(response.read())
            print(f"   GET /describe -> agents={list(described['agents'])} "
                  f"graphs={list(described.get('graphs', {}))}")
        request = urllib.request.Request(
            f"{base}/agents/assistant",
            data=json.dumps({"input": "what is the pricing?", "thread_id": "h1"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
            print(f"   POST /agents/assistant -> {body['text']!r}")
    finally:
        server.shutdown()
        server.server_close()
