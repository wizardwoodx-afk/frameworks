# RISTER

**One framework for governed AI agents.** Typed state graphs, an agent runtime, policy
enforcement, security scanning, tracing and evaluation — in a single dependency-free
Python package with one coherent set of primitives.

```bash
pip install -e .          # from this directory
python -m rister demo     # end-to-end walkthrough, no API key needed
```

> **See the architecture:** [`../docs/architecture.html`](../docs/architecture.html) — the
> module graph, the enforced import law, the six gates every tool call passes through, and a
> measured trace, all generated from this source tree.

- **Zero runtime dependencies.** Standard library only, Python 3.10+.
- **One vocabulary.** A `State` flows through a `Graph`, an `Agent` writes to it, a
  `Guard` authorises every tool call, a `Tracer` records what happened.
- **Deterministic where it matters.** Same input, same trace, same decisions. Model
  calls are the only non-deterministic part, and they are optional.
- **Governance is not a bolt-on.** Policy, identity, approvals, audit and sandboxing
  are first-class objects the agent loop already understands.
- **Product surface is brand-neutral.** No third-party names in the API, the CLI or the
  code — enforced by a test.

---

## Table of contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Orchestration](#1-orchestration--typed-state-graphs)
- [Agents](#2-agents--models-tools-memory)
- [Governance](#3-governance--policy-identity-approval-audit)
- [Security](#4-security--scanning-and-analysis)
- [Observability](#5-observability--spans-and-exporters)
- [Evaluation](#6-evaluation--cases-scorers-suites)
- [Serving](#7-serving--http-surface)
- [CLI](#cli)
- [Extending](#extending)
- [Design notes](#design-notes)
- [Testing](#testing)

---

## Quick start

```python
import rister as rs

class Draft(rs.State):
    topic: str = ""
    notes: list = rs.Append()       # append, order preserving
    score: int = rs.Sum()           # accumulate
    tags: list = rs.Unique()        # append without duplicates

graph = rs.Graph(Draft, "draft")
graph.add_node("research", lambda s: {"notes": [f"facts about {s.topic}"]})
graph.add_node("score", lambda s: {"score": 10, "tags": ["reviewed"]})
graph.add_edge(rs.START, "research")
graph.add_edge("research", "score")
graph.add_edge("score", rs.END)

result = graph.compile().invoke({"topic": "governance"})
print(result.values)
# {'topic': 'governance', 'notes': ['facts about governance'], 'score': 10, 'tags': ['reviewed']}
```

Add an agent, then guard its tools:

```python
@rs.tool
def search(query: str) -> str:
    """Search the internal notes store."""
    return f"results for {query}"

policy = rs.Policy.from_dict({
    "name": "prod", "default": "deny",
    "rules": [
        {"name": "read", "action": "search", "effect": "allow"},
        {"name": "publish", "action": "publish_*", "effect": "approve"},
    ],
})

guard = rs.Guard(policy=policy, audit=rs.AuditLog(key="signing-key"),
                 approver=rs.AutoApprover())

agent = rs.Agent(
    "analyst",
    model=rs.HTTPModel(model="local-llm", base_url="http://localhost:11434/v1"),
    instructions="You are a careful analyst.",
    tools=[search],
    memory=rs.Memory(rs.InMemoryStore(), window=20),
    guard=guard,          # every tool call is authorised first
)

print(agent.run("Summarise the governance notes").text)
```

### Runnable examples

| File | Shows |
|---|---|
| [`examples/01_graphs.py`](examples/01_graphs.py) | reducers, conditional routing, interrupt/resume, snapshot replay, `to_dot()` |
| [`examples/02_governed_agent.py`](examples/02_governed_agent.py) | tools, policy allow/deny/approve, audit chain, tamper detection, memory |
| [`examples/03_security_gate.py`](examples/03_security_gate.py) | scanning, recorded suppressions, SARIF, `SECURITY.md` drafting |
| [`examples/04_observability_and_evals.py`](examples/04_observability_and_evals.py) | nested traces, JSONL export, eval suites, live HTTP endpoint |

Each script runs offline and exits 0:

```bash
python examples/01_graphs.py
```

## Architecture

```
                         ┌───────────────────────────┐
        orchestration    │  State  →  Graph  →  run  │  supersteps, reducers,
                         │         snapshots         │  interrupts, replay
                         └────────────┬──────────────┘
                                      │ nodes may be agents
                         ┌────────────▼──────────────┐
        agents           │ Agent → Model → Tools     │  memory, structured
                         │        ↕  Memory          │  output, streaming
                         └────────────┬──────────────┘
                                      │ every tool call passes through
                         ┌────────────▼──────────────┐
        governance       │ Guard → Policy → Audit    │  identity, approvals,
                         │   Sandbox / Rings         │  tamper-evident log
                         └────────────┬──────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
      ┌───────▼────────┐     ┌────────▼────────┐     ┌────────▼────────┐
      │  security      │     │  observability  │     │  evaluation     │
      │  rules+scanner │     │  tracer+spans   │     │  cases+scorers  │
      └────────────────┘     └─────────────────┘     └─────────────────┘
                                      │
                         ┌────────────▼──────────────┐
                         │  serve: App over HTTP     │
                         └───────────────────────────┘
```

Every layer is usable alone. The arrows describe what composes with what, not a
required import order.

---

## 1. Orchestration — typed state graphs

**State** declares fields, each bound to a reducer that defines how updates merge:

| Reducer | Merge behaviour |
|---|---|
| `Last()` | replace (default) |
| `Append()` | extend a list, order preserving |
| `Unique()` | append items not already present |
| `Merge()` | shallow-merge mappings |
| `Sum()` | numeric accumulation |
| `Max()` / `Min()` | keep the extreme |

Nodes return **partial updates**, never mutate state. Unknown fields are rejected, so a
typo fails loudly instead of silently creating a key.

```python
graph = rs.Graph(Draft, "pipeline")
graph.add_node("research", research, retry=2)
```

| Capability | API |
|---|---|
| Static edges | `add_edge(START, "a")`, `add_edge("a", END)` |
| Conditional routing | `add_conditional_edges("a", router, {"yes": "b", "no": END})` |
| Self-routing nodes | `return rs.goto("b", update={"x": 1})` |
| Fan-out | `return rs.goto("l", "r")` — one superstep, merged deterministically |
| Sub-graphs | `add_subgraph("child", compiled)` |
| Retries | `add_node("flaky", fn, retry=3)` |
| Loop guard | `compile(max_steps=50)` |
| Durable state | `compile(store=rs.FileSnapshotStore("runs/"))` |
| Human in the loop | `ctx.interrupt(payload)` → `compiled.resume(value, config)` |
| Streaming | `compiled.stream(...)` → `run_start`, `node_start`, `node_end`, `run_end` |
| Time travel | `compiled.history(thread)`, `compiled.replay(thread, step)` |
| Diagram | `graph.to_dot()` |

Superstep semantics: every node in a frontier sees the **same** input state; updates are
merged in frontier order, so parallel branches cannot race.

### Human in the loop

```python
def approve(state, ctx):
    answer = ctx.interrupt({"question": f"publish {state.topic}?", "notes": state.notes})
    return {"approved": bool(answer)}

first = compiled.invoke({"topic": "budget"}, config={"thread_id": "t1"})
if first.interrupted:
    preview = first.interrupt           # the payload
    final = compiled.resume(True, {"thread_id": "t1"})
```

The pause is persisted: resuming works from another process, days later.

## 2. Agents — models, tools, memory

```python
agent = rs.Agent(
    "writer",
    model=backend,                       # any rs.Model
    instructions="Write tersely.",       # str | Prompt | callable | list
    tools=[search, publish],             # functions, rs.Tool or agent.as_tool()
    memory=rs.Memory(rs.InMemoryStore(), window=20),
    guard=guard,
    max_turns=8,
    output_schema=Report,                # structured output
    tracer=tracer,
)
result = agent.run("Draft the summary")
result.text, result.data, result.tool_calls, result.usage, result.denied
```

**Backends** — implement one method, `complete(messages, tools, **options) -> Completion`:

| Backend | Purpose |
|---|---|
| `ScriptedModel([...])` | deterministic replies for tests and demos |
| `CallableModel(fn)` | wrap any function |
| `HTTPModel(model=..., base_url=...)` | any `/chat/completions`-shaped endpoint (no vendor default; you must supply the URL) |
| `RouterModel({name: model}, choose=fn)` | route per request (cheap vs strong) |

**Tools** are typed closures with inferred JSON schema:

```python
@rs.tool(requires_approval=True, tags=["email"])
def send_email(to: str, body: str) -> str:
    """Send an email to one recipient."""
    ...

send_email.spec()          # name, description, JSON schema, flags
send_email.as_function_spec()   # chat-completions function declaration
```

**Memory** separates storage from policy: `InMemoryStore` / `FileMemoryStore` persist
messages per thread; `Memory(window=...)` limits what the model sees;
`SummarizingMemory` compresses older turns once history grows.

**Structured output** — pass a dataclass, `TypedDict` or JSON schema; the model is asked
for JSON, the reply is parsed and type-coerced, and missing keys mark the run
`incomplete` rather than returning junk.

Two integration helpers make agents composable:

```python
graph.add_node("write", agent.as_node(input_key="topic", output_key="output"))
parent = rs.Agent("lead", tools=[researcher.as_tool()], model=backend)
```

## 3. Governance — policy, identity, approval, audit

```python
policy = rs.Policy.from_dict({
    "name": "production",
    "default": "deny",
    "rules": [
        {"name": "read",   "action": ["read_*", "search"], "effect": "allow"},
        {"name": "unsafe", "action": ["delete_*", "drop_*"], "effect": "deny",
         "reason": "destructive operations are blocked"},
        {"name": "mail",   "action": "send_email", "effect": "approve"},
        {"name": "internal","action": "query_db", "effect": "allow",
         "when": {"args.env": "internal"}},
        {"name": "admins",  "action": "admin_*", "effect": "allow",
         "when": {"subject.roles": {"contains": "admin"}}},
    ],
})
```

| Piece | What it gives you |
|---|---|
| `Policy` / `PolicySet` | ordered rules, glob actions, argument and subject predicates, `allow` / `deny` / `approve` |
| `Guard` | single choke point: evaluate → approve → audit → allow or raise |
| `Identity`, `Signer`, `Directory` | stable actor ids, roles, HMAC-signed capability tokens |
| `QueueApprover` / `AutoApprover` / `CallbackApprover` | human sign-off for sensitive tools |
| `AuditLog` / `FileAuditLog` | hash-chained, optionally HMAC-signed, verifiable |
| `Sandbox`, `Ring` | rooted filesystem jail, path-escape refusal, capability tiers |
| `PromptDefense` | static audit of a system prompt against 12 injection vectors |
| `SupplyChainGuard` | unpinned, range and typosquat detection in dependencies |

The agent loop already understands the guard, so a denial is fed back to the model as
tool output instead of crashing the run:

```python
audit = rs.AuditLog(key="signing-key")
guard = rs.Guard(policy=policy, audit=audit, approver=rs.QueueApprover())
result = agent.run("clean up old rows", {"subject": rs.Identity("agent:ops")})
result.denied        # [{'tool': 'delete_rows', 'reason': '...'}]
audit.verify().ok    # True — and it detects any edit to a past entry
```

Sandboxing is explicit about capability tiers:

| Ring | read | write | exec | Typical use |
|---|---|---|---|---|
| `ROOT` | ✓ | ✓ | ✓ | the host process itself |
| `TRUSTED` | ✓ | ✓ | ✓ | first-party agent with vetted commands |
| `LIMITED` | ✓ | — | — | analysis agents |
| `UNTRUSTED` | — | — | — | pure computation, no I/O |

## 4. Security — scanning and analysis

```python
report = rs.scan(".")
print(report.summary())                  # 3 finding(s) in 120 file(s) (high 1, medium 2)
report.save("rister.sarif", fmt="sarif") # GitHub code scanning
```

- 18 built-in rules across secrets, injection, deserialisation, crypto, config, auth and
  agent-specific risks (prompt assembly, unauthorised tool dispatch).
- Outputs: JSON, **SARIF 2.1.0**, markdown, CSV — with stable fingerprints for
  de-duplication and baselining.
- Inline suppression: `# rister:ignore SEC002` (this line and the next) or
  `rister:ignore-file SEC015`. Suppressed findings are **recorded**, not dropped:
  `report.suppressions` keeps the rule, severity, path, line and directive that hid each
  one, so "we chose to accept this" stays visible and reviewable.
- `draft_security_policy(report)` produces a `SECURITY.md` grounded in what was actually
  found, rather than a generic template.

> A clean scan is not proof of security. Reports include a `coverage` section stating
> exactly what was and was not examined.

## 5. Observability — spans and exporters

```python
tracer = rs.Tracer("prod", exporters=[rs.JSONLExporter("traces.jsonl")])
compiled = graph.compile(tracer=tracer)     # graph, agent, model and tool spans nest
agent = rs.Agent("a", model=backend, tracer=tracer)

with tracer.span("job", ticket="ABC-1") as span:
    span.set("retries", 0)
    ...
tracer.summary()   # counts, totals, errors, per-span aggregates
```

Graph, agent, model **and tool** spans nest under one trace; interrupts are recorded as
`interrupted`, not `error`, so a paused run does not look like a failure.

## 6. Evaluation — cases, scorers, suites

```python
suite = rs.Suite(
    "support-quality",
    [
        {"name": "refund", "input": "I want a refund", "expected": ["refund"]},
        {"name": "math",   "input": "2+2",            "expected": 4},
    ],
    [rs.Contains(), rs.NumericClose()],
)
report = suite.run(agent)
print(report.summary())        # 2/2 passed (100%), mean score 1.000
report.to_markdown()
```

Deterministic scorers: `Exact`, `Contains`, `Regex`, `JsonKeys`, `NumericClose`, `Length`.
`ModelJudge(model, criteria)` wraps a model for subjective grading and is labelled as
non-reproducible.

## 7. Serving — HTTP surface

```python
app = rs.App(
    agents={"writer": agent},
    graphs={"pipeline": compiled},
    tracer=tracer,
    auth_token="service-token",     # optional bearer check
)
app.serve(host="0.0.0.0", port=8000)
```

| Route | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /describe` | agents, graphs, tools, routes |
| `POST /agents/<name>` | `{"input": "...", "thread_id": "..."}` |
| `POST /graphs/<name>` | `{"input": {...state fields}}` |
| `GET /traces` | trace summary |

Standard library `http.server`, JSON in/out, request size limits, and 400/401/404/413/422
status codes. Put TLS and a production server in front of it for real deployments.

## CLI

```bash
rister version
rister demo                                   # offline end-to-end walkthrough
rister scan . --format sarif --out rister.sarif
rister scan . --fail-on-severity high         # exit 1 for CI
rister policy lint policy.json
rister policy explain policy.json delete_rows --arg table=users
rister prompt check prompts/system.txt --min-grade B
rister deps check .
rister serve --port 8000
```

Exit codes: `0` success, `1` findings or policy failure, `2` usage/error.

## Extending

- **A new model backend**: subclass `rs.Model`, implement `complete`.
- **A new reducer**: subclass `rs.Reducer`, implement `reduce(current, value)`.
- **A new snapshot store**: implement `save` / `load` / `history` / `threads` / `clear`.
- **A new security rule**: append a `Rule` and register it; rules are data.
- **A new scorer**: subclass `rs.evals.Scorer`, implement `score(output, case)`.
- **A new exporter**: implement `export(payload)` and pass it to a `Tracer`.

## Design notes

1. **Merge over mutation.** Nodes return updates; reducers decide the algebra. This is
   what makes parallel branches, replays and snapshots deterministic.
2. **Fail loudly at the boundary.** Unknown state fields, unknown tools, unknown routes
   and unknown policy effects raise immediately with a list of valid values.
3. **Governance in the path, not around it.** Tool execution funnels through one method,
   which is where the guard sits. There is no second way to call a tool.
4. **Determinism by default.** No hidden clock, randomness or network in the core; every
   non-deterministic edge (model calls, subprocesses, registries) is opt-in and explicit.
5. **No vendor defaults.** The HTTP backend has no default URL or key name; you cannot
   accidentally call a third party.
6. **Composable, not monolithic.** Every subsystem imports and runs alone; the
   integrations are thin glue (`as_node`, `as_tool`, `guard=`).

## Testing

```bash
python -m rister.tests          # 80 tests, no third-party runner needed
python -m rister.tests -v      # verbose
python -m rister.tests govern   # one module
pytest rister/tests             # also works if pytest is installed
```

The suite covers reducers, graph semantics (interrupts, retries, subgraphs, replay),
agent loops, memory, structured output, policies, guards, audit tampering, sandboxes,
token expiry, prompt grading, supply-chain checks, the scanner (including suppressions),
SARIF shape, tracers, evaluators, the HTTP surface over real sockets, the CLI, and
debranding. `test_architecture.py` additionally parses the source with `ast` and fails the
build on an import cycle, an undocumented cross-module dependency, or a third-party
module-level import — see [`../docs/architecture.html`](../docs/architecture.html).

## Provenance

RISTER is a clean-room synthesis: the design draws on the capabilities of several
open-source agent frameworks, reimplemented behind one coherent API. No third-party
source code is included in this package. Attribution for the projects whose ideas
informed the design is in [`../THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md); the
framework itself carries no third-party branding, which
`rister/tests/test_debranding.py` enforces.
