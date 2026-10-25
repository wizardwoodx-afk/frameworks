# RISTER — how the seven layers became one framework

RISTER is not a wrapper and not a fork. It is a **clean-room synthesis**: the capabilities
that were scattered across six separate projects are reimplemented behind one coherent
API, with a single set of types, one error hierarchy and one vocabulary.

- **Package:** [`rister/`](../rister/) — Python 3.10+, zero runtime dependencies
- **Version:** 1.0.0
- **Tests:** 72 (including a suite that enforces brand-neutrality)
- **Try it:** `cd rister && python -m rister demo`

---

## 1. The merge: what was unified and what was chosen

The six vendored projects covered a lot of overlapping ground. Where they overlapped, the
best design won; where they did not, the capability was absorbed as a new subsystem.

| Capability | Overlap in the six | Decision in RISTER | Why |
|---|---|---|---|
| **Shared mutable state** | three distinct models: string-keyed dicts, Pydantic models, dataclass state | **typed `State` + reducer algebra** with strict field validation | typed fields catch typos; reducers make concurrent merges deterministic |
| **Execution model** | graph supersteps vs agent loops vs pipeline steps | **one graph engine**, agents are just nodes (`as_node`) | a single scheduler means one place for retries, snapshots and tracing |
| **Durability** | memory-only, JSON files, SQL databases, cloud snapshots | `SnapshotStore` interface + memory and file implementations | proves the contract without dragging in a database |
| **Human in the loop** | suspended graphs, approval queues, interrupt objects | `ctx.interrupt()` / `resume()` **plus** an approval queue for policy-driven sign-off | two real needs: pause a workflow, and authorise a tool |
| **Tool calling** | JSON-schema tools, Zod tools, decorated functions, MCP servers | one `Tool` with **inferred schema**, flags and a registry choke point | schema inference keeps the happy path short; the choke point is where governance lives |
| **Model access** | dozens of provider adapters each | **one method contract** (`complete`) + scripted/callable/HTTP/router backends | adapters are mechanical; the interesting part is the contract |
| **Memory** | buffers, windows, summaries, vector recall | **store vs policy split** (`InMemoryStore`/`FileMemoryStore` × `Memory`/`SummarizingMemory`) | persistence and prompt-shaping are different concerns |
| **Guardrails** | prompt-level safety, runtime policy engines, approval hooks | **deterministic policy engine** + `Guard` in the tool path | prompt-level safety is a request; enforcement belongs in code |
| **Identity** | shared API keys, signed tokens, agent registries | `Identity` + HMAC `Signer` + `Directory` | attribution per agent, not per deployment |
| **Audit** | logs, compliance exports, hash chains | **hash-chained, optionally HMAC-signed `AuditLog`** with verification | audit is only useful if tampering is detectable |
| **Sandboxing** | capability rings, filesystem jails, container sandboxes | `Ring` tiers + rooted `Sandbox` (no container dependency) | the ring model is portable; containers are an ops choice |
| **Prompt auditing** | static analysis of system prompts | `PromptDefense` over **12 vectors**, graded A–F | turns "is our prompt still safe?" into a CI gate |
| **Dependency hygiene** | supply-chain checks with registry lookups | offline **unpinned / range / typosquat** checks (Damerau-Levenshtein) | runs anywhere, no network, catches the common failures |
| **Code scanning** | agentic scanners, SARIF exporters, GitHub Actions | **18-rule deterministic scanner** with SARIF/JSON/MD/CSV + inline suppressions | fast, explainable, runs on every commit |
| **Observability** | console logs, OTel-style spans, hosted consoles | `Tracer` with nested spans and pluggable exporters | no exporter protocol lock-in |
| **Evaluation** | datasets + model judges + deterministic metrics | deterministic scorers first, `ModelJudge` clearly marked | CI needs reproducible numbers |
| **Serving** | Hono/Elysia/Express adapters, A2A/MCP servers | **stdlib HTTP `App`** with JSON routes | zero-dependency surface you can put behind any real server |
| **Structured output** | schema enforcement, JSON modes, validators | `output_schema` + parse/coerce/validate, `incomplete` status on failure | degraded output must be visible, not silent |

### What was deliberately **not** carried over

| Dropped | Reason |
|---|---|
| Provider adapter zoo (one per vendor) | mechanical duplication; the `complete` contract plus an HTTP backend covers it |
| Hosted console / cloud control plane | an operational product, not a framework concern |
| Monorepo build systems, codegen, bundlers | implementation detail of the sources, irrelevant to the merged API |
| Vector-database connectors | an integration concern; `Memory` takes any store you write |
| Container/deployer plugins | environment-specific; `Sandbox` and `App` are the portable cores |
| Framework-specific DSLs that duplicate each other | RISTER keeps one way to express each idea |

### Deliberate divergences from all of them

1. **Strict typed state.** Every source allowed either over-permissive dicts or rigid
   models. RISTER rejects unknown fields with the valid list in the message.
2. **Governance in the execution path.** Tool calls have exactly one entry point, so a
   policy cannot be bypassed by calling the function directly through the registry.
3. **No vendor defaults.** The HTTP backend refuses to construct without an explicit
   `base_url`; there is no baked-in endpoint or key name.
4. **Interrupts are not errors.** Traces mark paused runs `interrupted`, so dashboards do
   not report expected control flow as failure.
5. **Scanner suppressions.** Inline `# rister:ignore RULE` directives make it possible to
   keep a clean CI signal without disabling a rule globally.

---

## 2. Layered architecture

```
orchestration   State · reducers · Graph · supersteps · snapshots · interrupts
     │
agents          Agent · Model backends · Tools · Memory · structured output
     │
governance      Policy · Guard · Identity · Approval · Audit · Sandbox · Prompt/Deps audit
     │
analysis        Security scanner · SARIF · Evals · Scorers
     │
plumbing        Tracer/Exporters · serving App · CLI · demo
```

Imports flow downward only. `core` (errors, messages, tool calls, config) has no
dependencies at all, which is why every other module can import it without cycles.

### Module map

| Module | Contents |
|---|---|
| `rister.core` | `Message`, `ToolCall`, `Usage`, `Completion`, `Config`, 17 exception types |
| `rister.state` | `State`, `Reducer`, `Last/Append/Unique/Merge/Sum/Max/Min` |
| `rister.graph` | `Graph`, `CompiledGraph`, `NodeContext`, `Command`, snapshots, engine |
| `rister.agent` | `Agent`, `Model` family, `Tool`/`ToolRegistry`, memory, prompts |
| `rister.govern` | `Policy`, `Guard`, `Identity`/`Signer`, approvals, `AuditLog`, `Sandbox`, `PromptDefense`, `SupplyChainGuard` |
| `rister.security` | `Scanner`, `Rule`s, `ScanReport`, SARIF, `SECURITY.md` drafting |
| `rister.observe` | `Tracer`, `Span`, exporters |
| `rister.evals` | `Case`, `Scorer`s, `Suite`, `Report` |
| `rister.serve` | `App` over `http.server` |
| `rister.demo` | the offline walkthrough |

---

## 3. The three invariants

Everything else is a consequence of these.

**(1) State is merged, never mutated.**
```python
result = compiled.invoke({"notes": ["a"]})     # nodes return {"notes": ["b"]}
# reducers decide that "a" and "b" concatenate; the input object is untouched
```

**(2) Every tool call passes through one method.**
```python
registry.call(name, arguments, config=..., guard=...)   # authorise, then execute
```
Agents use it. Graph nodes that call tools use it. There is no second path.

**(3) Every decision is recorded and verifiable.**
```python
guard.check("delete_rows")   # deny -> audit entry -> PolicyDenied
audit.verify()               # detects any edit to any past entry
```

---

## 4. Composition recipes

**A governed research pipeline**

```python
class Pipeline(rs.State):
    question: str = ""
    findings: list = rs.Append()
    answer: str = ""
    cost: int = rs.Sum()

graph = rs.Graph(Pipeline, "research")
graph.add_node("search", agent_search)        # agent using a read-only tool
graph.add_node("analyse", analyst_agent.as_node(input_key="question", output_key="answer"))
graph.add_node("review", approval_node)       # ctx.interrupt(...) for sign-off
graph.add_edge(rs.START, "search")
graph.add_edge("search", "analyse")
graph.add_edge("analyse", "review")
graph.add_edge("review", rs.END)
pipeline = graph.compile(store=rs.FileSnapshotStore("runs"), tracer=tracer)
```

**Evaluation in CI**

```python
report = rs.Suite("q3", cases, [rs.Contains(), rs.JsonKeys()]).run(agent)
assert report.pass_rate >= 0.9, report.to_markdown()
```

**Security gate in CI**

```bash
rister scan . --fail-on-severity high --format sarif --out rister.sarif
rister prompt check prompts/system.txt --min-grade B
rister deps check .
```

These three commands are wired into this repository's CI.

---

## 5. Performance and footprint

- **Import cost:** pure Python, no compiled extensions; `import rister` loads the whole
  surface (use `import rister.graph` etc. for a leaner import).
- **Graph overhead:** a superstep is a dict merge per frontier node; a 3-node pipeline
  runs in tens of microseconds, excluding node bodies and model calls.
- **Scanner:** a 38-file package scans in ~60 ms; files above `max_file_bytes` are skipped
  and counted, never silently ignored.
- **Memory:** snapshots are plain dicts; the file store is append-only JSON lines.

## 6. Limits, stated plainly

- The HTTP surface is for local development and trusted networks. Add TLS and a hardened
  server for production.
- The security rules are heuristics. A clean report means "no known patterns", not
  "secure".
- `ModelJudge` is non-deterministic by construction; keep it out of gating thresholds.
- Sandboxing is in-process path and capability control, not OS-level isolation. Use
  containers when you need kernel isolation.
- The `FileSnapshotStore`/`FileMemoryStore` assume single-writer semantics per thread.
