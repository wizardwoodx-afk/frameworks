# Agent Frameworks — vendored, pinned, verified

Six production agent frameworks and security SDKs, cloned into this repository as **git submodules**, pinned to their latest stable releases, documented, and backed by **smoke tests that actually execute framework code**.

| # | Framework | Language | Pinned version | License | Upstream |
|---|-----------|----------|----------------|---------|----------|
| 1 | **LangGraph** | Python (+ TS) | `1.2.17` | MIT | [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) |
| 2 | **Mastra** | TypeScript | `@mastra/core@1.79.0` | Apache-2.0 ¹ | [mastra-ai/mastra](https://github.com/mastra-ai/mastra) |
| 3 | **BeeAI Framework** | Python + TS | `python_v0.1.85` | Apache-2.0 | [i-am-bee/beeai-framework](https://github.com/i-am-bee/beeai-framework) |
| 4 | **VoltAgent** | TypeScript | `@voltagent/core@2.11.0` | MIT | [VoltAgent/voltagent](https://github.com/VoltAgent/voltagent) |
| 5 | **Microsoft AGT** | Python/TS/Go/Rust/.NET | `v4.1.0` | MIT | [microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit) |
| 6 | **OpenAI Codex Security** | TypeScript | `npm-v0.6.0` | Apache-2.0 | [openai/codex-security](https://github.com/openai/codex-security) |

¹ Mastra is Apache-2.0 except content under `ee/` directories, which carries a separate commercial license.

**All six are verified working in this repo** — installed from the pinned releases and exercised with real code, not just imports. See [docs/verification.md](docs/verification.md) for the exact commands and captured output.

```
langgraph        11/11 checks passed  (graph run, checkpoint round-trip, conditional routing)
beeai            3/3                  (async workflow, agent + tool surface)
agt              4/4                  (gov runtime, prompt-defence, CLI)
mastra           3/3                  (instance + component registry)
voltagent        3/3                  (202 exports, Agent constructs)
codex-security   2/2                  (CLI v0.6.0 executes)
```

---

## Quick start

```bash
# 1. Materialise the six upstream repositories (pick ONE)
./scripts/bootstrap.sh --shallow     # depth-1, ~526 MB   <- fastest
./scripts/bootstrap.sh               # full history, ~2.3 GB

# 2. Install the pinned packages and prove everything runs
./scripts/verify.sh                  # creates .venv, pip + npm install, smoke tests

# 3. Check pins whenever you come back
./scripts/bootstrap.sh --status
```

Requirements: `git`, `python3 >= 3.11` (BeeAI requires it), `node >= 22.13` (Codex Security requires it), `npm`.

Per-framework details, architecture notes, and runnable examples live in [`docs/`](docs/):

| Doc | Covers |
|-----|--------|
| [docs/00-overview.md](docs/00-overview.md) | Comparison matrix — what each one is actually for, and how to choose |
| [docs/01-langgraph.md](docs/01-langgraph.md) | Graphs, Pregel, checkpointing, human-in-the-loop |
| [docs/02-mastra.md](docs/02-mastra.md) | Agents, workflows, memory, MCP, monorepo layout |
| [docs/03-beeai-framework.md](docs/03-beeai-framework.md) | ReAct/tool-calling agents, workflows, serve (A2A/ACP/MCP) |
| [docs/04-voltagent.md](docs/04-voltagent.md) | Agent runtime, supervisors, server adapters, sandboxes |
| [docs/05-agent-governance-toolkit.md](docs/05-agent-governance-toolkit.md) | Policy enforcement, identity, sandboxing, SRE |
| [docs/06-codex-security.md](docs/06-codex-security.md) | CLI + TypeScript SDK, SARIF, GitHub Action |
| [docs/verification.md](docs/verification.md) | Exactly what was run and what it printed |

---

## Why submodules instead of committed source?

Because these six repositories are enormous, and committing them would destroy this repo:

| Metric | Measured |
|--------|----------|
| Files across all six | **32,566** total (**23,529** source files) |
| Shallow (depth-1) checkouts | **526 MB** |
| Full git histories | **~2.3 GB** |

GitHub also reports Mastra at ~1.6 GB packed on its own. Vendoring that into this repository would exceed practical Git and artifact limits by an order of magnitude, and would make every clone painful.

So each framework is a **gitlink** — a pinned commit SHA plus upstream URL in [`.gitmodules`](.gitmodules) — the standard Git mechanism for exactly this. What that gives you:

- **Exact provenance** — every framework is pinned to a specific immutable commit, recorded twice: in the gitlink and in [`MANIFEST.json`](MANIFEST.json).
- **Full source on demand** — `./scripts/bootstrap.sh` performs a real clone (full history by default), not a vendored copy. You can `git log`, `git blame`, diff branches, and pull upstream commits directly.
- **No duplicated objects** — publishing an update or rebasing doesn't rewrite 2 GB of vendored code.
- **Clean licensing boundary** — upstream `LICENSE` files travel with each clone, untouched.

Tabs, not subdirectories: `vendor/mastra` *is* `mastra-ai/mastra`, checked out at `4c22623`.

---

## Layout

```
.
├── MANIFEST.json              machine-readable pins: tag, SHA, date, license, size
├── .gitmodules                the six submodule URLs
├── README.md                  this file
├── THIRD-PARTY-NOTICES.md     license + attribution summary
├── Makefile                   make bootstrap | verify | status | pins | clean
├── docs/                      one deep-dive per framework + verification log
├── scripts/
│   ├── bootstrap.sh           clone/update submodules (--shallow, --only, --jobs, --status)
│   ├── verify.sh              install pinned packages + run smoke tests
│   ├── sync-pins.sh           detect/apply upstream release drift
│   └── _apply_pins.py         helper for --write
├── smoke/
│   ├── python/smoke_test.py   11 functional checks (LangGraph + BeeAI + AGT)
│   └── node/smoke.mjs         8 functional checks (Mastra + VoltAgent + Codex Security)
└── vendor/                    submodule mount points (populated by bootstrap)
    ├── langgraph/
    ├── mastra/
    ├── beeai-framework/
    ├── voltagent/
    ├── agent-governance-toolkit/
    └── codex-security/
```

---

## Working with the vendored sources

```bash
# inspect a framework exactly as pinned
cd vendor/langgraph && git log -1 && git describe --tags

# pull upstream changes for one framework (then re-pin deliberately)
cd vendor/mastra && git fetch origin && git log --oneline HEAD..origin/main | head

# update pins to the newest upstream release
./scripts/sync-pins.sh              # report drift
./scripts/sync-pins.sh --write      # update MANIFEST.json + print git update-index commands
./scripts/bootstrap.sh --status     # confirm what's checked out
```

`scripts/sync-pins.sh` never silently moves a pin: it resolves each upstream's latest release, prints a drift table, and only mutates `MANIFEST.json` when you pass `--write`.

## Known, expected noise

- **Mastra** prints `No 'storage' configured on Mastra — falling back to an in-memory store` in the smoke test. That is correct: the test constructs a bare `new Mastra({})` with no storage adapter.
- **VoltAgent** may log a `ModelProviderRegistry auto-refresh failed` warning when the sandbox has no route to `models.dev`. Module loading and agent construction still succeed.
- **AGT modules are split across PyPI extras.** `pip install agent-governance-toolkit` provides the `agent_compliance` runtime and the `agt` CLI; heavier components (`agent-os`, `agentmesh`, `agent-sre`) are separate packages under `vendor/agent-governance-toolkit/agent-governance-python/` and some arrive via extras such as `[full]`.

## Licensing

Each framework keeps its own upstream license inside `vendor/<name>/LICENSE`. This repository's own scripts and docs add no restrictions beyond attribution. Summary in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

| Framework | License | Note |
|-----------|---------|------|
| LangGraph | MIT | © LangChain, Inc. |
| Mastra | Apache-2.0 | `ee/` subdirectories are separately licensed |
| BeeAI Framework | Apache-2.0 | LF AI & Data project |
| VoltAgent | MIT | © VoltAgent Inc. |
| Microsoft AGT | MIT | © Microsoft Corporation |
| Codex Security | Apache-2.0 | © OpenAI |
