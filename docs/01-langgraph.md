# 1. LangGraph

> Low-level orchestration framework for stateful, long-running agents.

- **Pinned:** tag `1.2.17` → commit `4c52e129eb1ab69a3da58b799f67929f77d81149` (2026-10-24)
- **Upstream:** https://github.com/langchain-ai/langgraph
- **License:** MIT · © 2024 LangChain, Inc.
- **Submodule path:** `vendor/langgraph`

## What it is

LangGraph models an agent as a **graph of nodes over shared typed state**. You declare the
state schema, the nodes that transform it, and the edges (fixed or conditional) between
them. Execution is a Pregel-style bulk-synchronous loop: each *superstep* runs the active
nodes, applies their state updates through per-key reducers, then advances. That is what
makes cycles, retries, time travel, and human interrupts first-class rather than bolted on.

The key mental shift: **a node returns a partial state update, it does not mutate state.**
Channels (one per state key) decide how concurrent updates combine — this is where
`Annotated[list, add_messages]`-style reducers live.

## Layout at the pinned commit

```
vendor/langgraph/
├── libs/
│   ├── langgraph/langgraph/      # the core engine
│   │   ├── graph/                # StateGraph, compile()
│   │   ├── pregel/               # superstep scheduler, retry, durability
│   │   ├── channels/             # state reducers & topics
│   │   ├── checkpoint/           # persistence interfaces
│   │   ├── func/                 # @task / @entrypoint functional API
│   │   ├── runtime.py            # Runtime, context
│   │   └── types.py              # Command, Send, interrupt
│   ├── checkpoint/               # base checkpointer + InMemorySaver
│   ├── checkpoint-sqlite/
│   ├── checkpoint-postgres/
│   ├── checkpoint-conformance/   # cross-implementation test suite
│   ├── prebuilt/                 # create_react_agent & friends
│   ├── cli/                      # `langgraph` dev server / gen
│   ├── sdk-py/  sdk-js/          # client SDKs for LangGraph Server
│   └── ...
├── docs/      ├── examples/      └── Makefile
```

## Install

```bash
pip install langgraph==1.2.17            # core only
pip install langgraph-checkpoint-sqlite  # + durable local persistence
pip install langgraph-checkpoint-postgres
```

## Verified example — graph run + checkpoint read-back

This exact snippet is executed by [`smoke/python/smoke_test.py`](../smoke/python/smoke_test.py):

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END

class S(TypedDict):
    n: int
    trail: list[str]

def inc(s: S): return {"n": s["n"] + 1, "trail": s["trail"] + ["inc"]}
def dbl(s: S): return {"n": s["n"] * 2, "trail": s["trail"] + ["dbl"]}

g = StateGraph(S)
g.add_node("inc", inc)
g.add_node("dbl", dbl)
g.add_edge(START, "inc")
g.add_edge("inc", "dbl")
g.add_edge("dbl", END)
app = g.compile()

print(app.invoke({"n": 5, "trail": []}))   # {'n': 12, 'trail': ['inc', 'dbl']}
```

Durable state (verified):

```python
from langgraph.checkpoint.memory import InMemorySaver

app = g.compile(checkpointer=InMemorySaver())
cfg = {"configurable": {"thread_id": "smoke-1"}}
app.invoke({"n": 1, "trail": []}, cfg)

state = app.get_state(cfg)
print(state.values)   # state restored from the checkpoint
print(state.next)     # () when the graph finished
```

Swap `InMemorySaver` for `SqliteSaver`/`PostgresSaver` and the same `thread_id` gives you
crash-safe resumption — that is the "durable execution" story.

## Conditional routing and interrupts

```python
g.add_conditional_edges("grade", lambda s: s["route"], {"pass": "on_pass", "fail": "on_fail"})
```

For human-in-the-loop, call `interrupt(payload)` inside a node; the graph pauses, the
checkpoint persists, and you resume with `Command(resume=value)`:

```python
from langgraph.types import interrupt, Command

def approval(state):
    answer = interrupt({"question": "Deploy to prod?", "draft": state["draft"]})
    return {"approved": answer}

app.invoke(Command(resume=True), cfg)   # continues from the paused node
```

Other primitives worth reading up on in `libs/langgraph/langgraph/`:

- `Send` — fan out to N parallel node invocations (map-reduce over dynamic work lists).
- `Command(goto=..., update=...)` — combine routing and state update in one return value.
- `Runtime` / `context` — dependency injection and run-scoped context.
- `stream()` — `values`, `updates`, `messages`, `custom`, `debug` stream modes.
- `libs/langgraph/langgraph/func/` — the functional `@entrypoint`/`@task` API if you prefer
  imperative code over explicit graphs.

## Where to start reading

1. `libs/langgraph/langgraph/graph/state.py` — `StateGraph`, `compile()`.
2. `libs/langgraph/langgraph/pregel/` — the superstep loop and retry/durability logic.
3. `libs/langgraph/langgraph/channels/` — how state updates merge (the reducer contract).
4. `libs/checkpoint/langgraph/checkpoint/base/__init__.py` — the checkpointer interface you
   must implement for a new backend.
5. `examples/` — multi_agent, reflection, plan-and-execute, LATS, ReWOO, human_in_the_loop.

## Gotchas

- `pip install langgraph` is **core only**. Persistence needs a separate
  `langgraph-checkpoint-*` package; `examples/` may require extra deps you'd install yourself.
- Nodes must return *updates*, not the full state. Returning new list objects without an
  `add_messages`-style reducer silently overwrites history instead of appending.
- The JS version is a **separate repository** ([langgraphjs](https://github.com/langchain-ai/langgraphjs));
  `libs/sdk-js` here is only the client SDK for LangGraph Server.
- Replay/"time travel" only works if the graph is deterministic between checkpoints —
  pin your prompts and avoid un-seeded randomness inside nodes.
