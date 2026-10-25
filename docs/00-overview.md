# Overview — six frameworks, one repo

These six projects overlap but are not interchangeable. This page is the map: what each
one is actually *for*, its mental model, and how the pieces fit if you use several together.

| | **LangGraph** | **Mastra** | **BeeAI** | **VoltAgent** | **MS AGT** | **Codex Security** |
|---|---|---|---|---|---|---|
| Primary job | Stateful graph orchestration | TypeScript agent platform | Multi-agent framework (Py + TS) | TS agent runtime + platform | Agent **governance** | Security **scanning** |
| Language | Python (+ JS SDK) | TypeScript | Python + TypeScript | TypeScript | Py, TS, Go, Rust, .NET | TypeScript + CLI |
| Mental model | Nodes + edges + channels | Agents + workflows + Mastra registry | ReAct / tool-calling agents + Workflow | Agents + supervisors + tools | Interceptors on tool calls | Scan → findings → patch |
| Durable execution | Checkpointers (memory/SQLite/Postgres) | Snapshot + storage adapters | Memory / workflow state | Resumable streams + memory adapters | Audit chain | Scan history |
| Human-in-the-loop | `interrupt()` / `Command` | `suspend`/`resume` | Middleware | Hooks / approvals | Policy allow/deny | Report-only by default |
| Runtime surface | Library (+ LangGraph Platform) | Library + Mastra server | Library + `serve` (A2A/ACP/MCP) | Library + Hono/Elysia servers | Kernel + CLI + SDKs | CLI + SDK + GitHub Action |
| Install | `pip install langgraph` | `npm i @mastra/core` | `pip install beeai-framework` | `npm i @voltagent/core` | `pip install agent-governance-toolkit` | `npm i @openai/codex-security` |

## The two layers

Four of these build agents. Two of them wrap around whatever you built:

```
        ┌──────────────────────────────────────────────────────────┐
        │  Governance & assurance layer                            │
        │  MS AGT  (runtime policy, identity, audit, sandboxing)   │
        │  Codex Security (static/agentic vulnerability scanning)  │
        └──────────────────────────────────────────────────────────┘
        ┌──────────────────────────────────────────────────────────┐
        │  Agent construction layer                                │
        │  LangGraph (Py)   Mastra (TS)                            │
        │  BeeAI (Py/TS)    VoltAgent (TS)                         │
        └──────────────────────────────────────────────────────────┘
```

- **AGT** is not an agent framework. It intercepts tool calls, messages, and delegations
  *before* they reach the wire, so it composes with the other four (or with your own code).
- **Codex Security** is not a runtime dependency at all — it is a scanning tool you point at
  a repository (including, recursively, this one) to find and patch vulnerabilities.

## Choosing between the four agent frameworks

- **LangGraph** — you want explicit control: typed state, cycles, conditional edges,
  deterministic replay from checkpoints, and the ability to interrupt mid-run for a human.
  Lowest abstraction, highest control. Python-first; pick this if your stack is Python.
- **Mastra** — you want an opinionated full TS platform in one dependency graph: agents,
  workflows, memory, RAG, evals, logging, MCP, deployers, plus a `Mastra` registry that
  wires agents/workflows together and exposes them over HTTP. Largest surface area here.
- **BeeAI** — you want production-shaped agents across **both** languages with the same
  concepts, plus out-of-the-box interoperability (`serve` exposes agents over A2A/ACP/MCP).
  Good fit when you need protocol interop or a Python/TS polyglot team.
- **VoltAgent** — you want a TS runtime with supervisors/sub-agents, pluggable memory
  (libSQL/Postgres/Supabase), sandboxed execution (E2B/Daytona/Blaxel), and strong
  observability (VoltOps, Langfuse/Vercel AI exporters) without adopting a whole platform.

## Composing them

A realistic combination this repo supports:

1. Build the agent in **LangGraph** (Python) or **VoltAgent/Mastra** (TypeScript).
2. Gate its tool calls with **AGT**'s policy/interceptor surface and record an audit trail.
3. Serve it via **BeeAI**'s `serve` (A2A) or VoltAgent's Hono server if you need a network API.
4. Run **Codex Security** over the repository in CI, exporting SARIF to code scanning.
5. Export traces to **Mastra** observability or **VoltAgent**'s exporters, depending on stack.

## Version and license reality check

All six are pinned to their latest **stable** release as of 2026-10-25 and are recorded in
[`MANIFEST.json`](../MANIFEST.json). Two caveats worth knowing before you ship:

- **AGT** is explicitly a *Public Preview* (`v4.1.0`); upstream warns of breaking changes
  before GA. Its PyPI installer ships `agent_compliance` plus the `agt` CLI; the ~20
  component packages under `agent-governance-python/` are released separately.
- **Mastra** is Apache-2.0 *except* `ee/` paths, which are under a separate commercial
  license. Do not copy those directories into a product without reading `ee/LICENSE`.

Per-framework deep dives: [LangGraph](01-langgraph.md) · [Mastra](02-mastra.md) ·
[BeeAI](03-beeai-framework.md) · [VoltAgent](04-voltagent.md) · [AGT](05-agent-governance-toolkit.md) ·
[Codex Security](06-codex-security.md)
