# 3. BeeAI Framework

> Build production-ready multi-agent systems in Python or TypeScript.

- **Pinned:** tag `python_v0.1.85` → commit `0304ca17c975e315e4867f697d49057e64c60764` (2026-09-28)
- **Upstream:** https://github.com/i-am-bee/beeai-framework
- **License:** Apache-2.0 (LF AI & Data Foundation project)
- **Submodule path:** `vendor/beeai-framework`

## What it is

BeeAI ships the **same conceptual model in two languages**, side by side:
`python/beeai_framework/` and `typescript/src/`. That symmetry is the selling point — a
Python and a TypeScript team can share vocabulary (agents, tools, workflows, memory,
middleware, serve) instead of translating between ecosystems.

Three layers are worth separating:

1. **Agents** — `agents/react/` (reason+act loop), `agents/tool_calling/`, `agents/requirement/`,
   `agents/lite/`. Pick by how much control you want over the prompt loop.
2. **Workflows** — typed, step-based orchestration with a Pydantic `BaseModel` state,
   for when you want determinism instead of an agent deciding the next step.
3. **`serve/`** — exposes an agent over **A2A**, **ACP**, or **MCP**, so it is callable by
   other agents rather than only by your app. This is BeeAI's differentiator.

## Layout at the pinned commit

```
vendor/beeai-framework/
├── python/
│   ├── beeai_framework/
│   │   ├── agents/          # react/, tool_calling/, requirement/, lite/
│   │   ├── backend/         # ChatModel + provider backends
│   │   ├── tools/           # built-in tools (weather, search, code, think, ...)
│   │   ├── workflows/       # Workflow + typed steps
│   │   ├── memory/  cache/  middleware/  emitters/  parsers/
│   │   ├── serve/           # A2A / ACP / MCP servers
│   │   ├── evaluation/      # judges / metrics
│   │   └── template.py  runnable.py  context.py
│   ├── examples/  tests/  pyproject.toml
├── typescript/src/          # mirror of the above (adapters, agents, backend, ...)
├── docs/  docs-old/         # current + legacy docs
└── mise.toml  CITATION.cff  LICENSE
```

## Install

```bash
pip install beeai-framework==0.1.85     # Python; requires 3.11 <= version < 3.14
npm install beeai-framework              # TypeScript
```

## Verified example — typed workflow (no API key needed)

Executed by [`smoke/python/smoke_test.py`](../smoke/python/smoke_test.py). Note two API
details that will bite you: the state **must be a Pydantic `BaseModel`** (a dataclass is
rejected by validation), and `wf.run(...)` returns an **awaitable `Run`**, so you await it
rather than `asyncio.run(wf.run(...))`.

```python
import asyncio
from pydantic import BaseModel, Field
from beeai_framework.workflows import Workflow

class St(BaseModel):
    x: int = 0
    trail: list[str] = Field(default_factory=list)

wf = Workflow(St)

def inc(s: St):
    s.x += 1
    s.trail.append("inc")

def dbl(s: St):
    s.x *= 2
    s.trail.append("dbl")

wf.add_step("inc", inc)
wf.add_step("dbl", dbl)

async def main():
    out = await wf.run(St(x=3))
    print(out.state.model_dump())     # {'x': 8, 'trail': ['inc', 'dbl']}

asyncio.run(main())
```

## Verified surface — agent + tool + model

```python
from beeai_framework.agents.react import ReActAgent   # ReAct loop
from beeai_framework.backend import ChatModel         # provider-agnostic chat model
from beeai_framework.tools.weather.openmeteo import OpenMeteoTool
#                                                  ^ OpenMeteoTool, not "OpenMeteoWeatherTool"

print(OpenMeteoTool().name)   # 'OpenMeteoTool'
```

Wiring an agent typically looks like: construct a `ChatModel` for your provider → give the
agent tools → `await agent.run(prompt).on(...)` for streaming events. A live LLM call needs
credentials; the smoke test deliberately stops at the import/construct boundary so it runs
offline.

## Serving an agent (A2A / ACP / MCP)

`beeai_framework.serve` turns an agent into a network service. Conceptual shape:

```python
from beeai_framework.serve import ...  # A2A/ACP/MCP server factories
# build your agent, wrap it in the server, expose over the protocol
```

Because ACP was donated to the Linux Foundation and folded into **A2A** (per the upstream
README's 2025/08 note), treat A2A as the long-lived protocol to target for new work.

## Where to start reading

1. `python/beeai_framework/agents/react/` — the ReAct loop and its prompt scaffolding.
2. `python/beeai_framework/workflows/workflow.py` — step execution and typed state.
3. `python/beeai_framework/backend/` — how a `ChatModel` abstracts providers.
4. `python/beeai_framework/serve/` — A2A/ACP/MCP exposure.
5. `typescript/src/` — same concepts if your team is TS-first.

## Gotchas

- **Python version window is narrow:** `>=3.11,<3.14`. Python 3.10 will fail to install.
- `Workflow(State)` requires a **Pydantic** model; plain dataclasses raise a
  `ValidationError` inside `WorkflowRun`.
- `wf.run()` returns a `Run` object (awaitable). Passing it to `asyncio.run()` raises
  `ValueError: a coroutine was expected`.
- Built-in tool class names are literal (`OpenMeteoTool`); the docs and older examples
  sometimes use different names. Check `beeai_framework/tools/<category>/` before importing.
- The repo contains both `docs/` and a legacy `docs-old/`; prefer `docs/`.
