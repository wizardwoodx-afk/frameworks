# 4. VoltAgent

> AI agent engineering platform built on an open-source TypeScript agent framework.

- **Pinned:** tag `@voltagent/core@2.11.0` → commit `72a46c76b4507e1f98b41d5050a92bd30587eb70` (2026-09-28)
- **Upstream:** https://github.com/VoltAgent/voltagent
- **License:** MIT · © 2025 VoltAgent Inc.
- **Submodule path:** `vendor/voltagent`

## What it is

Two layers under one project name:

- **The OSS framework** — `@voltagent/core` plus a modular set of packages for memory,
  RAG, servers, sandboxes, evals, and exporters. You keep full code control.
- **VoltOps Console** — the hosted/self-hosted observability + operations product. Not
  required to run agents; it is where traces, evals, prompts, and guardrails live.

Architecturally VoltAgent is a **runtime** rather than a graph engine: an `Agent` owns
instructions, a model, tools, and memory; a **Supervisor** can route across **sub-agents**;
workflows describe multi-step automations declaratively. Tooling is Zod-typed with
lifecycle hooks and cancellation support.

## Layout at the pinned commit

```
vendor/voltagent/
├── packages/
│   ├── core/                  # Agent, supervisor, tools, memory, workflows
│   ├── server-core/ server-hono/ server-elysia/ serverless-hono/
│   ├── libsql/ postgres/ supabase/ cloudflare-d1/   # memory adapters
│   ├── rag/ voice/ evals/ scorers/ resumable-streams/
│   ├── sandbox-e2b/ sandbox-daytona/ sandbox-blaxel/  # isolated execution
│   ├── mcp-server/ a2a-server/ ag-ui/ sdk/
│   ├── vercel-ai-exporter/ langfuse-exporter/ logger/
│   ├── anthropic-ai/ google-ai/ groq-ai/            # provider packages
│   ├── cli/ create-voltagent-app/ docs-mcp/ internal/ shared/ e2e/
├── examples/  docs/  i18n/
└── lerna.json  nx.json  pnpm-workspace.yaml  biome.json
```

## Install

```bash
npm install @voltagent/core@2.11.0
npm install @voltagent/libsql             # memory persistence
npm install @voltagent/server-hono        # HTTP server adapter
npx create-voltagent-app@latest           # scaffold
```

## Verified surface at 2.11.0

The smoke test asserts the package loads and an agent can be constructed offline:

```ts
import { Agent } from '@voltagent/core';

const agent = new Agent({
  name: 'smoke-agent',
  instructions: 'You are a smoke test.',
  model: { provider: 'openai', modelId: 'gpt-4o-mini', toolChoice: 'auto' },
});

console.log(agent.name);   // 'smoke-agent'
```

`@voltagent/core@2.11.0` exposes **202 named symbols**, including `Agent`, `AgentRegistry`,
`A2AServerRegistry`, `BaseRetriever`, `CompositeFilesystemBackend`, embedding adapters,
conversation and error types. A representative agent with a real model call looks like:

```ts
import { Agent } from '@voltagent/core';
import { openai } from '@ai-sdk/openai';
import { z } from 'zod';

const weatherTool = {
  name: 'get_weather',
  description: 'Get the weather for a city',
  parameters: z.object({ city: z.string() }),
  execute: async ({ city }) => ({ city, tempC: 31, condition: 'clear' }),
};

const assistant = new Agent({
  name: 'assistant',
  instructions: 'Answer using tools when helpful.',
  model: openai('gpt-4o-mini'),
  tools: [weatherTool],
  memory: /* a memory adapter, e.g. @voltagent/libsql */ undefined,
});

const res = await assistant.generateText('Weather in Chennai?');
console.log(res.text);
```

(That inner call needs an `OPENAI_API_KEY`; the offline smoke test stops before it.)

## Supervisors and sub-agents

The pattern to reach for when one agent is not enough: a supervisor agent receives the task,
delegates to specialised sub-agents, and reconciles their answers. In the tree this lives in
`packages/core/src/agent/` (sub-agent registration + delegation) — check that directory for
the current option names, since these APIs are the most actively evolving part of the repo.

## Serving

Pick a server adapter instead of hand-rolling HTTP:

| Adapter | Package |
|---|---|
| Hono | `@voltagent/server-hono` |
| Elysia | `@voltagent/server-elysia` |
| Hono on serverless runtimes | `@voltagent/serverless-hono` |
| Agent-to-Agent protocol | `@voltagent/a2a-server` |
| MCP server exposure | `@voltagent/mcp-server` |

## Where to start reading

1. `packages/core/src/agent/` — the agent loop, tool calls, hooks.
2. `packages/core/src/memory/` — the memory interface the adapters implement.
3. `packages/server-hono/` — how agents become HTTP endpoints.
4. `packages/core/src/workflow/` — declarative multi-step orchestration.
5. `examples/` — the most reliable source of current, working wiring.

## Gotchas

- **Importing `@voltagent/core` triggers a background model-registry refresh** against
  `models.dev`. Offline you'll see `[ModelProviderRegistry] Auto-refresh failed: TypeError:
  fetch failed` on stderr — harmless, the module still works (this is exactly what the
  smoke test tolerates).
- `@voltagent/core` does **not** expose `./package.json` via its `exports` map, so
  `require.resolve('@voltagent/core/package.json')` throws. Read the file from
  `node_modules` instead.
- 2.x is a major-version line; `next` dist-tag is already on 3.0.0-next. Pin exactly, as
  this repo does, before starting a migration.
- Memory adapters are separate packages. Passing no `memory` means no persistence across runs.
