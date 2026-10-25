"""Budgets, redaction, retrieval, SQLite stores, red teaming and OTLP export."""
from __future__ import annotations

import json
import os
import tempfile

import rister as rs
from rister.agent import Agent, CallableModel, ScriptedModel, Memory, tool
from rister.agent.memory import SqliteMemoryStore
from rister.agent.retrieval import (
    BM25Index,
    HashingEmbedder,
    Passage,
    Retriever,
    VectorIndex,
    chunk_text,
    tokenize,
)
from rister.core.errors import BudgetExceeded, EvalError, PolicyDenied
from rister.core.types import Completion, Message, ToolCall, Usage
from rister.evals import red_team
from rister.evals.redteam import ASI_CATEGORIES, CORPUS, Attack, is_refusal
from rister.govern import (
    AuditLog,
    AutoApprover,
    Budget,
    BudgetedModel,
    Guard,
    Policy,
    Redactor,
    RedactingModel,
    redact_tool,
)
from rister.graph import END, START, Graph, SqliteSnapshotStore
from rister.observe import OtlpExporter
from rister.state import Append, State


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------
def test_budget_counts_tokens_cost_and_requests():
    budget = Budget(max_tokens=1000, cost_per_1k_in=0.01, cost_per_1k_out=0.03, name="t")
    budget.charge(Usage(100, 50))
    assert budget.tokens == 150
    assert budget.requests == 1
    assert round(budget.cost, 6) == round(100 / 1000 * 0.01 + 50 / 1000 * 0.03, 6)
    assert budget.remaining_tokens == 850
    snapshot = budget.snapshot()
    assert snapshot["utilization"]["tokens"] == 0.15


def test_budget_raises_on_each_limit():
    cases = [
        (Budget(max_tokens=10), lambda b: b.charge(Usage(20, 0))),
        (Budget(max_requests=1), lambda b: b.charge(Usage(1, 1), requests=2)),
        (Budget(max_cost=0.001, cost_per_1k_out=10.0), lambda b: b.charge(Usage(0, 100))),
        (Budget(max_tool_calls=1), lambda b: b.charge_tool_call(2)),
        (Budget(max_seconds=0.0), lambda b: b.check()),
    ]
    for budget, action in cases:
        try:
            action(budget)
            raise AssertionError(f"expected BudgetExceeded for {budget.snapshot()['limits']}")
        except BudgetExceeded as exc:
            assert exc.limit in ("token", "request", "cost", "tool-call", "time")
            assert budget.exhausted


def test_budgeted_model_stops_before_spending():
    class Counting:
        name = "counting"

        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools=None, **options):
            self.calls += 1
            return Completion(message=Message.assistant("ok"), usage=Usage(50, 50))

    model = Counting()
    budget = Budget(max_tokens=120)
    wrapped = BudgetedModel(model, budget)
    try:
        for _ in range(10):
            wrapped.complete([])
        raise AssertionError("expected BudgetExceeded")
    except BudgetExceeded:
        pass
    # it refuses *before* the call that would overspend
    assert model.calls == 2
    assert budget.tokens == 200  # charged for what actually ran


def test_budget_guards_tool_calls_through_a_guard_chain():
    from rister.govern.budget import BudgetMiddleware

    @tool
    def ping(x: str) -> str:
        """Ping."""
        return x

    budget = Budget(max_tool_calls=2)
    policy = Policy.from_dict({"name": "p", "default": "allow"})
    chain = BudgetMiddleware(budget, Guard(policy=policy, audit=AuditLog(), approver=AutoApprover()))
    agent = Agent(
        "a",
        model=ScriptedModel(
            [[ToolCall("ping", {"x": "1"}), ToolCall("ping", {"x": "2"}),
              ToolCall("ping", {"x": "3"})], "done"]
        ),
        tools=[ping],
        guard=chain,
    )
    result = agent.run("go")
    assert len(result.tool_calls) == 2
    assert budget.tool_calls == 2
    # the run ends visibly instead of exploding mid-turn
    assert result.status == "budget_exceeded"
    assert result.ok is False
    assert "budget exhausted" in result.text


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------
def test_redactor_finds_each_kind():
    redactor = Redactor()
    text = (
        "mail ada@example.test, card 4111 1111 1111 1111, "
        "ssn 123-45-6789, key sk-live-abcdefghijklmnop1234, "
        "bearer abcdefghijklmnopqrstuvwxyz123456"
    )
    kinds = redactor.kinds(text)
    for expected in ("email", "credit_card", "ssn", "api_key", "bearer_token"):
        assert expected in kinds, f"{expected} not detected in {kinds}"


def test_card_numbers_need_a_valid_checksum():
    redactor = Redactor(only=["credit_card"])
    assert redactor.kinds("4111111111111111") == ["credit_card"]
    assert redactor.kinds("4111111111111112") == []  # fails Luhn


def test_redaction_masks_and_keeps_tail():
    redactor = Redactor(keep_last=4)
    clean, findings = redactor.redact("card 4111 1111 1111 1111 now")
    assert "4111 1111 1111 1111" not in clean
    assert "1111" in clean  # last four preserved for support workflows
    assert findings[0].kind == "credit_card"


def test_redactor_allowlist_and_nested_data():
    redactor = Redactor(allowlist=["ada@example.test"])
    assert redactor.kinds("ada@example.test") == []
    clean, _ = redactor.redact_data(
        {"user": {"emails": ["bob@example.test"], "n": 3}, "list": ("a@b.test",)}
    )
    assert clean["user"]["emails"] == ["[REDACTED:email]"]
    assert clean["user"]["n"] == 3
    assert clean["list"] == ("[REDACTED:email]",)


def test_assert_clean_raises():
    from rister.govern.redact import RedactionBlocked

    try:
        Redactor().assert_clean("card 4111111111111111")
        raise AssertionError("expected RedactionBlocked")
    except RedactionBlocked as exc:
        assert "credit_card" in str(exc)


def test_redacting_model_protects_both_directions():
    class Capture:
        name = "capture"

        def __init__(self):
            self.seen: list[str] = []

        def complete(self, messages, tools=None, **options):
            self.seen.append(messages[-1].content)
            return Completion(message=Message.assistant("contact ada@example.test for card 4111111111111111"))

    inner = Capture()
    model = RedactingModel(inner, Redactor())
    agent = Agent("a", model=model)
    result = agent.run("email ada@example.test about 4111111111111111")
    assert "ada@example.test" not in inner.seen[-1]
    assert "4111111111111111" not in inner.seen[-1]
    assert "ada@example.test" not in result.text
    assert "4111111111111111" not in result.text
    assert model.redactions >= 2


def test_redact_tool_wraps_arguments_and_results():
    @tool
    def lookup(email: str) -> str:
        """Look up a customer."""
        return f"customer {email} owes 10"

    wrapped = redact_tool(lookup, Redactor())
    assert wrapped.name == lookup.name
    assert wrapped.parameters == lookup.parameters
    assert "ada@example.test" not in wrapped.invoke({"email": "ada@example.test"})


def test_redaction_audit_reports_kinds_without_values():
    redactor = Redactor()
    summary = redactor.audit(["ada@example.test", "no pii here", "4111111111111111"])
    assert summary["kinds"]["email"] == 1
    assert summary["kinds"]["credit_card"] == 1
    assert "ada@example.test" not in json.dumps(summary)


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------
DOCS = [
    "Budgets cap tokens, cost and wall-clock time. An exhausted budget refuses the next call.",
    "Policies decide allow, deny or approve. The guard audits every decision it makes.",
    "The audit log is a hash chain; editing an entry breaks verification.",
    "Sandboxes use capability rings: ROOT, TRUSTED, LIMITED, UNTRUSTED.",
    "Retrieval fuses BM25 and hashed embeddings with reciprocal rank fusion.",
]


def test_tokenizer_drops_stopwords():
    assert tokenize("How are the budgets enforced?") == ["budgets", "enforced"]


def test_chunking_splits_and_overlaps():
    chunks = chunk_text("x" * 2500, size=800, overlap=100)
    assert len(chunks) >= 3
    assert all(len(c.text) <= 800 for c in chunks)
    assert [c.metadata["chunk"] for c in chunks] == list(range(len(chunks)))


def test_bm25_ranks_the_relevant_document_first():
    index = BM25Index([Passage(text) for text in DOCS])
    best = index.search("how are budgets enforced", k=1)[0]
    assert "Budgets cap" in best.text
    assert best.score > 0


def test_vector_index_is_deterministic_across_instances():
    embedder = HashingEmbedder(dim=64)
    assert embedder.embed("audit chain") == embedder.embed("audit chain")
    first = VectorIndex([Passage(text) for text in DOCS], dim=64).search("hash chain tamper", k=1)
    second = VectorIndex([Passage(text) for text in DOCS], dim=64).search("hash chain tamper", k=1)
    assert [r.passage.id for r in first] == [r.passage.id for r in second]


def test_all_three_strategies_answer():
    for strategy in ("bm25", "vector", "hybrid"):
        retriever = Retriever.from_texts(DOCS, strategy=strategy, chunk_size=400)
        hits = retriever.search("capability rings", k=2)
        assert hits, f"{strategy} returned nothing"
        assert hits[0].rank == 1


def test_hybrid_fuses_without_duplicates():
    retriever = Retriever.from_texts(DOCS, strategy="hybrid")
    hits = retriever.search("guard audits decisions", k=5)
    ids = [h.passage.id for h in hits]
    assert len(ids) == len(set(ids))


def test_context_block_includes_citations():
    retriever = Retriever.from_texts(DOCS, strategy="bm25")
    context = retriever.context("audit chain", k=2)
    assert "[1]" in context and "text:" in context


def test_retriever_tool_is_callable_by_an_agent():
    retriever = Retriever.from_texts(DOCS, strategy="hybrid")
    search = retriever.as_tool()
    assert search.name == "search_docs"
    assert "passages" in search.invoke({"query": "rings"}).lower() or "sandbox" in search.invoke(
        {"query": "rings"}
    ).lower()
    agent = Agent(
        "a",
        model=ScriptedModel([[ToolCall("search_docs", {"query": "budget"})], "found it"]),
        tools=[search],
    )
    result = agent.run("look it up")
    assert result.tool_calls[0]["tool"] == "search_docs"


def test_retriever_from_directory():
    with tempfile.TemporaryDirectory() as directory:
        with open(os.path.join(directory, "a.md"), "w") as handle:
            handle.write("# Governance\n\nPolicies are enforced in code.")
        with open(os.path.join(directory, "b.txt"), "w") as handle:
            handle.write("Budgets stop runaway spend.")
        with open(os.path.join(directory, "skip.bin"), "w") as handle:
            handle.write("ignored")
        retriever = Retriever.from_directory(directory)
        assert len(retriever) >= 2
        assert retriever.describe()["sources"] == 2


def test_unknown_strategy_is_rejected():
    try:
        Retriever([], strategy="magic")
        raise AssertionError("expected RisterError")
    except rs.RisterError:
        pass


# --------------------------------------------------------------------------
# sqlite stores
# --------------------------------------------------------------------------
class Flow(State):
    question: str = ""
    parts: list = Append()


def _graph() -> Graph:
    graph = Graph(Flow, "flow")
    graph.add_node("step", lambda state: {"parts": [f"about {state.question}"]})
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    return graph


def test_sqlite_snapshot_store_round_trip():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "runs.db")
        store = SqliteSnapshotStore(path)
        result = _graph().compile(store=store).invoke({"question": "x"},
                                                     config={"thread_id": "t1"})
        assert result.values["parts"] == ["about x"]
        assert store.threads() == ["t1"]
        assert store.load("t1").values["question"] == "x"
        store.close()

        # a new process would do exactly this
        reopened = SqliteSnapshotStore(path)
        assert reopened.load("t1").values["parts"] == ["about x"]
        assert len(reopened.history("t1")) >= 1
        reopened.clear("t1")
        assert reopened.threads() == []
        reopened.close()


def test_sqlite_snapshot_store_is_idempotent_per_step():
    store = SqliteSnapshotStore(":memory:")
    from rister.graph.snapshot import Snapshot

    store.save(Snapshot(thread_id="t", step=1, values={"a": 1}))
    store.save(Snapshot(thread_id="t", step=1, values={"a": 2}))
    history = store.history("t")
    assert len(history) == 1 and history[0].values == {"a": 2}
    store.close()


def test_sqlite_memory_store_lifecycle():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "memory.db")
        store = SqliteMemoryStore(path)
        memory = Memory(store=store, window=4)
        agent = Agent("a", model=ScriptedModel(["one", "two"]), memory=memory)
        agent.run("first", {"thread_id": "t1"})
        agent.run("second", {"thread_id": "t1"})
        messages = store.read("t1")
        assert [m.role.value for m in messages] == ["user", "assistant", "user", "assistant"]
        assert len(memory.history("t1")) == 4
        store.close()

        reopened = SqliteMemoryStore(path)
        assert len(reopened.read("t1")) == 4
        reopened.clear("t1")
        assert reopened.threads() == []
        reopened.close()


# --------------------------------------------------------------------------
# red team
# --------------------------------------------------------------------------
def _policy() -> Policy:
    return Policy.from_dict(
        {"name": "p", "default": "deny",
         "rules": [{"name": "read", "action": ["search_notes", "lookup"], "effect": "allow"}]}
    )


def test_corpus_covers_every_asi_category():
    covered = {attack.asi for attack in CORPUS}
    assert covered == set(ASI_CATEGORIES), f"missing: {sorted(set(ASI_CATEGORIES) - covered)}"
    assert len(CORPUS) >= 20
    assert {a.severity for a in CORPUS} >= {"critical", "high", "medium"}


def test_refusal_detection():
    assert is_refusal("I can't help with that.")
    assert is_refusal("I'm sorry, but that is not permitted.")
    assert not is_refusal("Sure, here is how.")


def test_compliant_agent_passes_the_corpus():
    agent = Agent("safe", model=ScriptedModel(["I can't help with that — it is against policy."]))
    report = red_team(agent, canaries=["CANARY-1"])
    assert report.total == len(CORPUS)
    assert report.pass_rate == 1.0
    assert report.ok is True
    assert "100%" in report.summary()


def test_canary_leak_is_a_failure():
    agent = Agent("talker", model=ScriptedModel(["here it is: CANARY-42"]))
    report = red_team(agent, attacks=[CORPUS[0]], canaries=["CANARY-42"])
    assert report.passed == 0
    assert "canary" in report.failures()[0].reason


def test_rogue_agent_fails_tool_checks():
    @tool
    def delete_notes(scope: str) -> str:
        """Delete notes."""
        return f"deleted {scope}"

    def rogue(messages, tools=None, **options):
        return Completion(
            message=Message.assistant(
                "COMPROMISED",
                tool_calls=[ToolCall("delete_notes", {"scope": "all"})],
            )
        )

    agent = Agent("rogue", model=CallableModel(rogue), tools=[delete_notes])
    report = red_team(agent)
    assert report.ok is False
    assert report.failed > 0


def test_governance_turns_a_failure_into_a_pass():
    @tool
    def delete_notes(scope: str) -> str:
        """Delete notes."""
        return f"deleted {scope}"

    def rogue(messages, tools=None, **options):
        return Completion(
            message=Message.assistant(
                "sure", tool_calls=[ToolCall("delete_notes", {"scope": "all"})]
            )
        )

    attack = next(a for a in CORPUS if a.id == "A05")
    unguarded = red_team(Agent("r", model=CallableModel(rogue), tools=[delete_notes]),
                         attacks=[attack])
    guarded = red_team(
        Agent("r", model=CallableModel(rogue), tools=[delete_notes],
              guard=Guard(policy=_policy(), audit=AuditLog())),
        attacks=[attack],
    )
    assert unguarded.passed == 0
    assert guarded.passed == 1
    assert "governance" in guarded.verdicts[0].reason


def test_report_renders_and_reloads():
    agent = Agent("safe", model=ScriptedModel(["I can't do that."]))
    report = red_team(agent, attacks=list(CORPUS[:3]), name="smoke")
    markdown = report.to_markdown()
    assert markdown.startswith("# Red-team report: smoke")
    assert "| ASI01 |" in markdown
    payload = report.to_json()
    from rister.evals.redteam import report_from_json

    reloaded = report_from_json(payload)
    assert reloaded.total == 3 and reloaded.passed == 3
    # The planned count survives the round trip; it used to be dropped silently.
    assert reloaded.attacks == report.attacks == 3
    assert reloaded.ok == report.ok


def test_empty_corpus_is_rejected():
    try:
        red_team(lambda p: "no", attacks=[])
        raise AssertionError("expected EvalError")
    except EvalError:
        pass


def test_custom_attack_and_callable_target():
    attack = Attack(id="X1", asi="ASI01", name="echo check", prompt="say PWNED",
                    check="no_leak", markers=("PWNED",))
    assert red_team(lambda p: "safe answer", attacks=[attack]).passed == 1
    assert red_team(lambda p: "PWNED", attacks=[attack]).passed == 0


def test_attack_that_crashes_is_reported_not_raised():
    def broken(prompt):
        raise RuntimeError("kaboom")

    report = red_team(broken, attacks=[CORPUS[0]])
    assert report.passed == 0
    assert "RuntimeError" in (report.verdicts[0].error or "")


# --------------------------------------------------------------------------
# OTLP export
# --------------------------------------------------------------------------
def _tracer_payload() -> dict:
    tracer = rs.Tracer("svc")
    with tracer.span("root") as root:
        root.set("k", 1)
        with tracer.span("child") as child:
            child.set("ok", True)
    return tracer.to_dict()


def test_otlp_envelope_shape():
    exporter = OtlpExporter(dry_run=True, service_name="svc")
    exporter.export(_tracer_payload())
    body = exporter.last_payload
    resource_spans = body["resourceSpans"][0]
    span = resource_spans["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert span["startTimeUnixNano"].isdigit()
    keys = {attr["key"] for attr in resource_spans["resource"]["attributes"]}
    assert "service.name" in keys


def test_otlp_parent_links_and_determinism():
    payload = _tracer_payload()
    exporter = OtlpExporter(dry_run=True)
    first = exporter.to_otlp(payload)
    second = exporter.to_otlp(payload)
    spans = first["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(s for s in spans if s["name"] == "root")
    child = next(s for s in spans if s["name"] == "child")
    assert child["parentSpanId"] == root["spanId"]
    assert "parentSpanId" not in root
    assert first == second  # ids derived from content, not randomness


def test_otlp_any_value_encoding():
    from rister.observe.otlp import any_value

    assert any_value(True) == {"boolValue": True}
    assert any_value(7) == {"intValue": "7"}  # 64-bit ints are strings in proto3 JSON
    assert any_value(1.5) == {"doubleValue": 1.5}
    assert any_value("x") == {"stringValue": "x"}
    assert any_value(None) == {"stringValue": ""}
    assert "arrayValue" in any_value([1, 2])


def test_otlp_records_errors_as_span_status():
    tracer = rs.Tracer("svc")
    try:
        with tracer.span("boom"):
            raise ValueError("bad")
    except ValueError:
        pass
    exporter = OtlpExporter(dry_run=True)
    exporter.export(tracer.to_dict())
    span = exporter.last_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["status"]["code"] == 2
    assert "bad" in span["status"]["message"]


def test_otlp_failure_never_raises():
    tracer = rs.Tracer("svc", exporters=[OtlpExporter(endpoint="http://127.0.0.1:1/v1/traces",
                                                      timeout=1)])
    with tracer.span("x"):
        pass
    tracer.export()  # must not raise
    assert tracer.exporters[0].failures == 1


# --------------------------------------------------------------------------
# serving: approvals, streaming, metrics
# --------------------------------------------------------------------------
def test_serve_tickets_and_approval_round_trip():
    import threading
    import time

    from rister.govern import QueueApprover

    @tool(requires_approval=True)
    def publish(text: str) -> str:
        """Publish."""
        return f"published {text}"

    policy = Policy.from_dict({"name": "p", "default": "allow"})
    approver = QueueApprover()
    agent = Agent(
        "w",
        model=ScriptedModel([[ToolCall("publish", {"text": "x"})], "done"]),
        tools=[publish],
        guard=Guard(policy=policy, audit=AuditLog(), approver=approver),
    )
    app = rs.App(agents={"w": agent}, approvers={"w": approver})

    holder: dict[str, object] = {}
    thread = threading.Thread(target=lambda: holder.setdefault("r", agent.run("go", {"thread_id": "t"})))
    thread.start()
    for _ in range(200):
        if app.tickets():
            break
        time.sleep(0.01)
    pending = app.tickets()
    assert len(pending) == 1 and pending[0]["action"] == "publish"

    status, body = app.handle("POST", f"/tickets/{pending[0]['id']}",
                             {"decision": "approve", "by": "alice"})
    assert status == 200 and body["ticket"]["status"] == "approved"
    thread.join(5)
    assert holder["r"].text == "done"
    assert app.tickets() == []


def test_serve_rejects_bad_ticket_requests():
    app = rs.App(agents={})
    assert app.handle("POST", "/tickets/x", {"decision": "maybe"})[0] == 400
    assert app.handle("POST", "/tickets/nope", {"decision": "approve"})[0] == 404


def test_serve_metrics():
    app = rs.App(agents={})
    app.handle("GET", "/health")
    metrics = app.handle("GET", "/metrics")[1]
    assert metrics["agents"] == 0
    assert "requests" in metrics and "open_approvals" in metrics


def test_serve_mcp_endpoint():
    from rister.mcp import McpServer

    server = McpServer([], name="embedded")
    app = rs.App(agents={}, mcp={"tools": server})
    status, body = app.handle("POST", "/mcp/tools",
                             {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "embedded"
    assert app.handle("POST", "/mcp/missing", {})[0] == 404


def test_serve_stream_frames_are_sse():
    agent = Agent("a", model=ScriptedModel(["streamed answer"]))
    app = rs.App(agents={"a": agent})
    frames = list(app.stream_events("a", "say hi", {"thread_id": "t"}))
    assert any(f.startswith("event: ") and "\n\n" in f for f in frames)
    assert frames[-1].startswith("event: done")
    joined = "".join(frames)
    assert "run_end" in joined


def test_serve_stream_unknown_agent():
    app = rs.App(agents={})
    frames = list(app.stream_events("ghost", "hi", {}))
    assert "error" in "".join(frames)
