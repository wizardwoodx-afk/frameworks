"""01 — Typed state graphs: reducers, conditional routing, interrupts, replay.

Run:  python examples/01_graphs.py
"""
from __future__ import annotations

import tempfile

import rister as rs


class Report(rs.State):
    topic: str = ""
    notes: list = rs.Append()      # concatenate, order preserving
    tags: list = rs.Unique()       # append without duplicates
    score: int = rs.Sum()          # accumulate numbers
    stage: str = "start"
    approved: bool = False


def research(state: Report) -> dict:
    return {
        "notes": [f"fact A about {state.topic}", f"fact B about {state.topic}"],
        "tags": ["researched"],
        "score": 5,
        "stage": "researched",
    }


def review(state: Report, ctx) -> dict:
    # Pausing is ordinary control flow: the payload reaches the caller, and the
    # value passed to resume() becomes the return value of interrupt().
    answer = ctx.interrupt({"question": f"publish notes on {state.topic}?",
                            "notes": state.notes})
    return {"approved": bool(answer), "tags": ["reviewed"]}


def route(state: Report) -> str:
    return "publish" if state.approved else "archive"


def publish(state: Report) -> dict:
    return {"notes": ["published"], "stage": "published"}


def archive(state: Report) -> dict:
    return {"notes": ["archived"], "stage": "archived"}


graph = rs.Graph(Report, "report")
graph.add_node("research", research)
graph.add_node("review", review)
graph.add_node("publish", publish)
graph.add_node("archive", archive)
graph.add_edge(rs.START, "research")
graph.add_edge("research", "review")
graph.add_conditional_edges("review", route, {"publish": "publish", "archive": "archive"})
graph.add_edge("publish", rs.END)
graph.add_edge("archive", rs.END)

with tempfile.TemporaryDirectory() as store_dir:
    compiled = graph.compile(store=rs.FileSnapshotStore(store_dir))

    print("1. run until the human checkpoint")
    paused = compiled.invoke({"topic": "governance"}, config={"thread_id": "r1"})
    print(f"   interrupted={paused.interrupted} stage={paused.values['stage']}")
    print(f"   question: {paused.interrupt['question']}")

    print("2. a human says yes")
    final = compiled.resume(True, {"thread_id": "r1"})
    print(f"   status={final.status} approved={final.values['approved']}")
    print(f"   notes={final.values['notes']}")
    print(f"   tags={final.values['tags']} score={final.values['score']}")

    print("3. replay the same thread from its snapshots")
    history = compiled.history("r1")
    steps = [snap.step for snap in history]
    print(f"   snapshots recorded: {len(history)} at steps {steps}")
    first = compiled.replay("r1", steps[0])
    print(f"   step {steps[0]}: stage={first.values['stage']} notes={first.values['notes']}")

print("4. the route is inspectable as a diagram")
assert "review" in graph.to_dot()
print("   to_dot() rendered", len(graph.to_dot().splitlines()), "lines")
