# Debranding — the vocabulary translation

RISTER keeps the *ideas* and drops the *branding*. Every concept that came in under a
product-specific name got a neutral, descriptive name. This document records that
translation so anyone who knows the source terminology can find the equivalent, and so
new code has one obvious name to reach for.

The rule is simple: **a third-party name must not appear in the framework's code, its
public API, its CLI, or its documentation.** It is enforced, not aspirational.

```bash
python -m rister.tests debranding     # 7 tests, all fail if a name creeps back in
```

---

## 1. Concept translation

### Orchestration

| Source concept | RISTER | Note |
|---|---|---|
| graph / state graph | `Graph` | same idea, typed state only |
| node | `add_node` / `Node` | unchanged vocabulary |
| edge / conditional edge | `add_edge` / `add_conditional_edges` | unchanged |
| channel / reducer annotation | `Reducer` (`Last`, `Append`, …) | explicit objects instead of annotations |
| checkpointer | `SnapshotStore` (`MemorySnapshotStore`, `FileSnapshotStore`) | storage-agnostic contract |
| thread / config | `Config(thread_id=…)` | one config object for the whole run |
| interrupt / resume command | `ctx.interrupt(payload)` / `compiled.resume(value)` | straight-line node code |
| superstep / Pregel loop | superstep (same term) | bulk-synchronous frontier |
| Send / fan-out | `goto("a", "b")` | returns a list of targets |
| time travel / replay | `history()` / `replay(thread, step)` | reads the snapshot history |

### Agents and tools

| Source concept | RISTER | Note |
|---|---|---|
| agent | `Agent` | unchanged vocabulary |
| tool / function tool | `Tool`, `@tool` | schema inferred from annotations |
| tool schema (vendor wire shape) | `as_function_spec()` | neutral rendering |
| message rendering | `as_chat_message()` | neutral rendering |
| tool registry | `ToolRegistry` | single execution choke point |
| model provider / backend | `Model` (+ `HTTPModel`, `ScriptedModel`, …) | one contract, no vendor defaults |
| memory / conversation store | `Memory` + `MemoryStore` | policy and persistence separated |
| window / summarising memory | `Memory(window=…)`, `SummarizingMemory` | unchanged |
| instructions / system prompt | `instructions` (`str | Prompt | callable`) | fragments compose |
| structured output / schema mode | `output_schema` | parse + coerce + validate |
| agent handoff / agent-as-tool | `agent.as_tool()` | unchanged |
| agent as a graph step | `agent.as_node()` | unchanged |

### Governance

| Source concept | RISTER | Note |
|---|---|---|
| policy document | `Policy.from_dict(...)` / `.load(path)` | JSON canonical, YAML optional |
| policy rule | `Rule` (action glob + `when` predicates) | data, not code |
| policy engine / interceptor | `Guard` | one `check()` method |
| allow / deny / require approval | `ALLOW` / `DENY` / `APPROVE` decisions | `approve` is a first-class effect |
| agent identity / DID | `Identity` | id, kind, roles, attributes |
| capability token | `Signer.issue()` / `.verify()` | HMAC-SHA256, stdlib only |
| approval workflow | `QueueApprover`, `CallbackApprover`, `AutoApprover` | tickets with explicit decisions |
| audit chain / merkle log | `AuditLog` (`record`, `verify`) | hash-chained, optionally signed |
| execution ring | `Ring` (`ROOT`, `TRUSTED`, `LIMITED`, `UNTRUSTED`) | capability tiers |
| sandbox | `Sandbox` (rooted, path-escape refusal) | no container dependency |
| prompt defence / prompt audit | `PromptDefense`, `evaluate_prompt()` | 12 vectors, graded A–F |
| supply-chain guard | `SupplyChainGuard` | unpinned / range / typosquat |

### Analysis, observability, evaluation, serving

| Source concept | RISTER | Note |
|---|---|---|
| scanner / security scan | `Scanner`, `scan()` | 18 rules, SARIF export |
| finding / alert | `Finding` | stable fingerprint per finding |
| SARIF report | `report.to_sarif()` | 2.1.0, code-scanning ready |
| baseline / suppression | `# rister:ignore RULE` | inline, per line or per file |
| security policy draft | `draft_security_policy(report)` | generated from real findings |
| tracer / span | `Tracer`, `Span` | nested via context |
| exporter | `JSONLExporter`, `ConsoleExporter`, `CallableExporter` | pluggable |
| eval dataset / case | `Case`, `Suite` | unchanged vocabulary |
| metric / scorer | `Scorer` (`Exact`, `Contains`, …) | deterministic first |
| LLM judge | `ModelJudge` | explicitly marked non-reproducible |
| server / app | `App`, `create_app()` | stdlib HTTP |

---

## 2. What was removed, not renamed

Some things should not survive a merge at all:

| Removed | Why |
|---|---|
| Vendor-specific default endpoints and key environment variables | a framework must not silently call a third party |
| Provider adapter directories (one module per vendor) | the `complete()` contract makes them redundant |
| Hosted console / cloud control plane integrations | product surface, not framework |
| Monorepo tooling (task runners, bundlers, codegen, release bots) | irrelevant to the merged API |
| Duplicated DSLs for the same concept (e.g. two workflow syntaxes) | one way to express each idea |
| Telemetry that reports to a vendor by default | tracing is local unless you export it |

---

## 3. How it is enforced

`rister/tests/test_debranding.py` runs in CI and fails the build if:

1. any brand token appears in any `.py` file of the package;
2. any vendor URL (`github.com/`, `npmjs.com/`, `pypi.org/`) appears in the package;
3. any name in the public `__all__` export list contains a brand token;
4. any CLI subcommand contains a brand token;
5. the HTTP backend has a default vendor endpoint or key name;
6. the wire-format helpers are named after a vendor instead of their function;
7. `pyproject.toml` metadata mentions a brand.

The token list inside the test is assembled from fragments, so the auditor does not
trip its own scan — you can verify that by reading the file.

### Current status

```
$ python -m rister.tests debranding
7/7 passed
$ grep -ri -E "<brand tokens>" rister/ --include=*.py | wc -l
0
```

---

## 4. Where attribution lives instead

Removing names from the product does not remove the obligation to be honest about
influence. Attribution lives in one place, separated from the framework:

- [`../THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md) — the vendored projects, their
  licences, and a short note that RISTER's design draws on their capabilities.
- [`07-rister.md`](07-rister.md) — the merge rationale described **by capability**, which
  is the useful framing for anyone learning RISTER.

This keeps two things true at once: consumers of RISTER never inherit a third-party brand,
and the provenance of the ideas remains documented and checkable.
