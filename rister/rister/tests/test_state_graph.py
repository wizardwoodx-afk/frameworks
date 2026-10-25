"""State reducers and graph orchestration."""
from __future__ import annotations

import tempfile

from rister.core.errors import GraphError, NodeError, NotFound, StateError
from rister.graph import END, START, Command, FileSnapshotStore, Graph, goto
from rister.state import Append, Last, Max, Merge, Min, State, Sum, Unique


class Draft(State):
    topic: str = ""
    notes: list = Append()
    scores: dict = Merge()
    total: int = Sum()
    tags: list = Unique()
    best: int = Max()
    worst: int = Min()
    plain: int = 0


# -- state -----------------------------------------------------------------
def test_reducers_merge_incrementally():
    state = Draft(topic="t", notes=["a"], best=5)
    merged = state.update(
        {"notes": ["b"], "scores": {"k": 1}, "total": 3, "tags": ["x", "x"], "best": 2, "worst": 9}
    )
    assert merged.notes == ["a", "b"]
    assert merged.scores == {"k": 1}
    assert merged.total == 3
    assert merged.tags == ["x"]
    assert merged.best == 5
    assert merged.worst == 9


def test_state_updates_are_immutable():
    state = Draft(notes=["a"])
    state.update({"notes": ["b"]})
    assert state.notes == ["a"]


def test_unknown_field_rejected():
    for call in (lambda: Draft(nope=1), lambda: Draft().update({"nope": 1})):
        try:
            call()
            raise AssertionError("expected StateError")
        except StateError:
            pass


def test_state_subclass_inherits_fields():
    class Extended(Draft):
        extra: str = "hi"

    instance = Extended(topic="t")
    assert instance.notes == [] and instance.extra == "hi" and instance.topic == "t"
    assert set(Extended.fields()) >= set(Draft.fields())


def test_state_roundtrip():
    state = Draft(topic="x", notes=["a"])
    assert Draft.from_dict(state.to_dict()) == state


def test_last_reducer_replaces():
    assert Draft(plain=1).update({"plain": 2}).plain == 2
    assert isinstance(Draft.fields()["plain"], Last)


# -- graph -----------------------------------------------------------------
def _linear() -> Graph:
    graph = Graph(Draft, "linear")
    graph.add_node("inc", lambda s: {"plain": s.plain + 1, "notes": ["inc"]})
    graph.add_node("dbl", lambda s: {"plain": s.plain * 2, "notes": ["dbl"]})
    graph.add_edge(START, "inc")
    graph.add_edge("inc", "dbl")
    graph.add_edge("dbl", END)
    return graph


def test_graph_runs_in_order():
    result = _linear().compile().invoke({"plain": 5})
    assert result.ok
    assert result.values["plain"] == 12
    assert result.values["notes"] == ["inc", "dbl"]
    assert result.steps == 2


def test_conditional_routing_and_loop():
    graph = Graph(Draft, "loop")
    graph.add_node("bump", lambda s: {"plain": s.plain + 1})
    graph.add_edge(START, "bump")
    graph.add_conditional_edges(
        "bump", lambda s: "again" if s.plain < 3 else "stop", {"again": "bump", "stop": END}
    )
    assert graph.compile().invoke({}).values["plain"] == 3


def test_unknown_route_raises():
    graph = Graph(Draft, "bad")
    graph.add_node("a", lambda s: {})
    graph.add_edge(START, "a")
    graph.add_conditional_edges("a", lambda s: "missing", {"ok": END})
    try:
        graph.compile().invoke({})
        raise AssertionError("expected GraphError")
    except GraphError:
        pass


def test_max_steps_guard():
    graph = Graph(Draft, "spin")
    graph.add_node("spin", lambda s: {"plain": s.plain + 1})
    graph.add_edge(START, "spin")
    graph.add_conditional_edges("spin", lambda s: "spin", {"spin": "spin"})
    try:
        graph.compile(max_steps=5).invoke({})
        raise AssertionError("expected GraphError")
    except GraphError as exc:
        assert "max_steps" in str(exc)


def test_retry_budget():
    calls = {"n": 0}

    def flaky(state):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return {"plain": 42}

    graph = Graph(Draft, "retry")
    graph.add_node("flaky", flaky, retry=2)
    graph.add_edge(START, "flaky")
    graph.add_edge("flaky", END)
    assert graph.compile().invoke({}).values["plain"] == 42
    assert calls["n"] == 3


def test_retry_exhaustion_wraps_error():
    graph = Graph(Draft, "fail")
    graph.add_node("boom", lambda s: (_ for _ in ()).throw(RuntimeError("x")), retry=1)
    graph.add_edge(START, "boom")
    graph.add_edge("boom", END)
    try:
        graph.compile().invoke({})
        raise AssertionError("expected NodeError")
    except NodeError as exc:
        assert exc.node == "boom" and exc.attempts == 2


def test_interrupt_then_resume():
    graph = Graph(Draft, "approval")
    graph.add_node("prep", lambda s: {"notes": ["prepared"]})

    def human(state, ctx):
        return {"plain": 1 if ctx.interrupt({"ask": "ok?"}) else 0}

    graph.add_node("human", human)
    graph.add_edge(START, "prep")
    graph.add_edge("prep", "human")
    graph.add_edge("human", END)

    compiled = graph.compile()
    first = compiled.invoke({}, {"thread_id": "t1"})
    assert first.interrupted and first.interrupt == {"ask": "ok?"} and first.node == "human"
    assert compiled.get_state("t1").next == ["human"]

    second = compiled.resume(True, {"thread_id": "t1"})
    assert second.ok and second.values["plain"] == 1


def test_resume_unknown_thread_raises():
    compiled = _linear().compile()
    try:
        compiled.resume(True, {"thread_id": "nope"})
        raise AssertionError("expected NotFound")
    except NotFound:
        pass


def test_command_routing_and_fanout():
    graph = Graph(Draft, "cmd")
    graph.add_node("root", lambda s: goto("l", "r"))
    graph.add_node("l", lambda s: {"notes": ["l"]})
    graph.add_node("r", lambda s: {"notes": ["r"]})
    graph.add_edge(START, "root")
    graph.add_edge("l", END)
    graph.add_edge("r", END)
    result = graph.compile().invoke({})
    assert result.values["notes"] == ["l", "r"]


def test_stream_events():
    events = [e["event"] for e in _linear().compile().stream({"plain": 1})]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert events.count("node_start") == 2 and events.count("node_end") == 2


def test_file_store_history_and_replay():
    with tempfile.TemporaryDirectory() as directory:
        store = FileSnapshotStore(directory)
        compiled = _linear().compile(store=store)
        compiled.invoke({"plain": 2}, {"thread_id": "disk"})
        history = compiled.history("disk")
        assert [snap.step for snap in history] == [1, 2]
        assert compiled.get_state("disk").values["plain"] == 6
        assert compiled.replay("disk", 1).step == 1
        assert store.threads() == ["disk"]


def test_subgraph_mounting():
    inner = Graph(Draft, "inner")
    inner.add_node("plus5", lambda s: {"plain": s.plain + 5})
    inner.add_edge(START, "plus5")
    inner.add_edge("plus5", END)

    outer = Graph(Draft, "outer")
    outer.add_subgraph("child", inner.compile())
    outer.add_node("x2", lambda s: {"plain": s.plain * 2})
    outer.add_edge(START, "child")
    outer.add_edge("child", "x2")
    outer.add_edge("x2", END)
    assert outer.compile().invoke({"plain": 5}).values["plain"] == 20


def test_validation_errors():
    graph = Graph(Draft, "broken")
    graph.add_node("a", lambda s: {})
    graph.add_edge(START, "a")
    graph.add_edge("a", "ghost")
    try:
        graph.compile()
        raise AssertionError("expected GraphError")
    except GraphError as exc:
        assert "ghost" in str(exc)


def test_dot_export():
    dot = _linear().to_dot()
    assert dot.startswith("digraph") and "'inc' -> 'dbl'" in dot


def test_command_object_shape():
    command = Command(goto="next", update={"plain": 1})
    assert command.targets() == ["next"] and command.update == {"plain": 1}
