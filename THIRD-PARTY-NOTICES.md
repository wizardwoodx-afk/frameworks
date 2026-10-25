# Third-party notices

This repository does not contain the source code of the frameworks below in its own
history. Each is mounted as a git submodule at a pinned commit (see
[`MANIFEST.json`](MANIFEST.json) and [`.gitmodules`](.gitmodules)); running
`./scripts/bootstrap.sh` clones the upstream repositories in full, and their original
license files are preserved verbatim at `vendor/<name>/LICENSE*`.

The scripts, documentation, and smoke tests authored in this repository are provided
as-is for integration and evaluation purposes.

---

## 1. LangGraph
- Upstream: https://github.com/langchain-ai/langgraph
- Version pinned: `1.2.17` — commit `4c52e129eb1ab69a3da58b799f67929f77d81149` (2026-10-24)
- License: **MIT** — © 2024 LangChain, Inc.
- License file: `vendor/langgraph/LICENSE`

## 2. Mastra
- Upstream: https://github.com/mastra-ai/mastra
- Version pinned: `@mastra/core@1.79.0` — commit `4c22623ea342a0ce611e15f89f4ab8b377bf7ee8` (2026-10-21)
- License: **Apache-2.0**, with a carve-out: all content under directories named `ee/`
  (e.g. `@mastra/core/auth/ee`, `@mastra/core/agent-builder/ee`, `@mastra/editor/ee`)
  is licensed separately under the terms in [`ee/LICENSE`](https://github.com/mastra-ai/mastra/blob/main/ee/LICENSE).
  Treat those paths as **not** Apache-2.0 when redistributing.
- License files: `vendor/mastra/LICENSE.md`, `vendor/mastra/ee/LICENSE`

## 3. BeeAI Framework
- Upstream: https://github.com/i-am-bee/beeai-framework
- Version pinned: `python_v0.1.85` — commit `0304ca17c975e315e4867f697d49057e64c60764` (2026-09-28)
- License: **Apache-2.0** (LF AI & Data Foundation project)
- License file: `vendor/beeai-framework/LICENSE`

## 4. VoltAgent
- Upstream: https://github.com/VoltAgent/voltagent
- Version pinned: `@voltagent/core@2.11.0` — commit `72a46c76b4507e1f98b41d5050a92bd30587eb70` (2026-09-28)
- License: **MIT** — © 2025 VoltAgent Inc.
- License file: `vendor/voltagent/LICENCE`

## 5. Microsoft Agent Governance Toolkit (AGT)
- Upstream: https://github.com/microsoft/agent-governance-toolkit
- Version pinned: `v4.1.0` — commit `0de71ca6c95cf8b9b975ac96f48eaa7826bbe258` (2026-06-09)
- License: **MIT** — © Microsoft Corporation
- License file: `vendor/agent-governance-toolkit/LICENSE`
- Note: released as a **Public Preview**; the upstream project warns of potential
  breaking changes before general availability. Additional terms may apply to
  Microsoft trademarks (`TRADEMARKS.md` upstream).

## 6. OpenAI Codex Security
- Upstream: https://github.com/openai/codex-security
- Version pinned: `npm-v0.6.0` — commit `13dcc6fec00aaf4c23a6fa7e375ccc1726b22836` (2026-10-22)
- License: **Apache-2.0**
- License file: `vendor/codex-security/LICENSE`
- Note: using the CLI/SDK against OpenAI services requires an OpenAI account and is
  subject to OpenAI's separate terms of service. No credentials are stored here.

---

## Transitive dependencies

Installing the packages listed in `smoke/python/requirements.txt` and
`smoke/node/package.json` pulls their transitive dependency trees (e.g. `langchain-core`,
`pydantic`, `@ai-sdk/*`, `zod`). Those packages carry their own licenses; run
`pip-licenses` or `license-checker` in your environment if you need a full audit before
redistribution.
