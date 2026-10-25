# 2. Mastra

> The modern TypeScript framework for AI applications and agents.

- **Pinned:** tag `@mastra/core@1.79.0` → commit `4c22623ea342a0ce611e15f89f4ab8b377bf7ee8` (2026-10-21)
- **Upstream:** https://github.com/mastra-ai/mastra
- **License:** Apache-2.0, **except** `ee/` directories (see `vendor/mastra/LICENSE.md` + `ee/LICENSE`)
- **Submodule path:** `vendor/mastra`
- **Size note:** this is by far the largest of the six (~348 MB shallow, ~1.6 GB full history).

## What it is

Mastra is a platform-shaped framework: rather than one library you assemble, it ships a
whole dependency graph — agents, workflows, memory, RAG, evals, logging/tracing, MCP,
server adapters, and deployers — plus a `Mastra` **registry object** that holds your
agents/workflows/tools and exposes them over HTTP.

The important architectural detail at this version: the top level of the repository is
organised **by domain** (`workflows/`, `voice/`, `stores/`, `observability/`,
`server-adapters/`, `agent-sdks/`, `client-sdks/`, …), while `packages/` contains the
published npm artifacts (`@mastra/core`, `@mastra/memory`, `@mastra/rag`, `@mastra/mcp`,
`@mastra/deployer`, `@mastra/evals`, `create-mastra`, …) that re-export/assemble those
domains. Don't assume `packages/<x>` is the only place `<x>` is implemented.

## Layout at the pinned commit

```
vendor/mastra/
├── packages/              # published npm packages
│   ├── core/              # agents, workflows, tools, storage, MCP plumbing
│   ├── memory/  rag/  evals/  loggers/  mcp/  server/  deployer/
│   ├── cli/  create-mastra/  playground/  playground-ui/  auth/  connect/
│   └── _internals/ _vendored/ _test-utils/ ...
├── agent-sdks/            # agent-facing SDKs
├── client-sdks/           # browser/client SDKs
├── server-adapters/       # express/fastify/hono/... integrations
├── workflows/             # workflow engine + inngest + temporal runners
├── stores/                # storage adapters (pg, libsql, ...)
├── observability/         # tracing/telemetry
├── voice/  channels/  browser/  auth/  code-mode/  signals/  pubsub/
├── deployers/  embedders/  integrations/  workspaces/
├── ee/                    # ⚠ separately licensed (commercial)
├── docs/  examples/  templates/  e2e-tests/
└── pnpm-workspace.yaml  turbo.json  oxlint.config.ts
```

## Install

```bash
npm install @mastra/core@1.79.0
# typical companions
npm install @mastra/memory @mastra/libsql      # memory + storage adapter
npx create-mastra@latest                        # scaffold a new project
```

## Verified example — agent + workflow + registry

`@mastra/core`'s **default export surface is intentionally small** (it exports `Mastra`);
the rest is reached through subpaths. Verified against 1.79.0:

```ts
import { Mastra } from '@mastra/core';
import { Agent } from '@mastra/core/agent';
import { createWorkflow, createStep } from '@mastra/core/workflows';

// 1. An agent (needs a model string or a provider instance)
const agent = new Agent({
  name: 'demo',
  instructions: 'Be brief.',
  model: 'openai/gpt-4o-mini',
});

// 2. A workflow: steps are chained, then COMMITTED before they can run.
const double = createStep({
  id: 'double',
  execute: async ({ inputData }) => ({ value: inputData.value * 2 }),
});

const flow = createWorkflow({ id: 'demo-flow' }).then(double).commit();

const run = await flow.createRun();
const out = await run.start({ inputData: { value: 21 } });
console.log(out.status, out.result);   // success { value: 42 }

// 3. Register everything in one place
const mastra = new Mastra({ agents: { demo: agent }, workflows: { demoFlow: flow } });
mastra.getAgent('demo');
```

Two failure modes I hit while verifying, worth knowing:

- `new Mastra({})` warns `No 'storage' configured on Mastra — falling back to an in-memory
  store`. Fine for tests, **not durable** — configure `@mastra/libsql`/`@mastra/pg` for anything real.
- A workflow built with `.then(...)` **must** be followed by `.commit()` or the first run
  throws `Uncommitted step flow changes detected. Call .commit() to register the steps.`

## Concepts to know

| Concept | Where | Notes |
|---|---|---|
| Agents | `packages/core/src/agent/` | instructions, tools, memory; `generate()`/`stream()` |
| Workflows | `workflows/` + `packages/core` | step chaining, suspend/resume, snapshots |
| Memory | `packages/memory` | conversation history, working memory, semantic recall |
| Storage | `stores/`, `packages/core` | libsql / pg / d1 / cloudflare adapters |
| Tools & MCP | `packages/mcp`, `packages/core` | author MCP servers exposing agents/tools |
| Evals & scorers | `packages/evals` | dataset-backed evaluation |
| Observability | `observability/` | traces, spans, exporters |
| Deployers | `packages/deployer`, `deployers/` | Node, Cloudflare, Vercel, Netlify… |

## Where to start reading

1. `packages/core/src/agent/` — agent loop and tool-call handling.
2. `workflows/` — the workflow engine plus its Inngest/Temporal runners.
3. `packages/core/src/storage/` — the storage interface every adapter implements.
4. `packages/core/src/mcp/` — MCP client/server plumbing.
5. `examples/` + `templates/` — the fastest way to see intended structure.

## Gotchas

- **Do not vendor or redistribute `ee/`.** It is not Apache-2.0.
- The monorepo is pnpm + turbo; building from source is a heavy operation (22k files).
  If you only need the runtime, depend on the published packages.
- Deep imports into `dist/` are not stable; use documented subpaths
  (`@mastra/core/agent`, `/workflows`, `/tools`, `/memory`, …).
- The package tag in this repo (`@mastra/core@1.79.0`) is per-package — other `@mastra/*`
  packages in the same commit carry their own versions and may not be `1.79.0`.
