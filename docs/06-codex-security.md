# 6. OpenAI Codex Security

> Find, investigate, and fix vulnerabilities in your code — as a CLI, a TypeScript SDK,
> or a GitHub Action.

- **Pinned:** tag `npm-v0.6.0` → commit `13dcc6fec00aaf4c23a6fa7e375ccc1726b22836` (2026-10-22)
- **Upstream:** https://github.com/openai/codex-security
- **Docs:** `docs/` in the submodule (agent workflows, CLI reference, scanner imports)
- **License:** Apache-2.0
- **Submodule path:** `vendor/codex-security`

## What it is

An agentic security scanner. You point it at a repository (or a path, or a diff) and it
runs discovery workers that propose candidate vulnerabilities, then **validates** them,
optionally generates patches, and can verify existing fixes. Output is a Markdown report
plus findings in JSON, with SARIF/CSV export and optional publishing to Linear or a findings
service.

Distinct stance from the other five in this repo: Codex Security is **not** a runtime
dependency of your agent. It is a tool you run over code — including this repository.

## Layout at the pinned commit

```
vendor/codex-security/
├── sdk/typescript/          # @openai/codex-security SDK + CLI implementation
│   ├── src/  bin/  schemas/  docs/  tests-ts/  scripts/
├── plugins/codex-security/  # the Codex plugin form of the same workflows
├── github-action/  action.yml
├── docs/                    # agent-workflows.md, cli.md, scanner-imports/...
├── examples/  evals/  docker/
├── compose.yaml  compose.apparmor.yaml  compose.runner.yaml
└── Dockerfile  RELEASING.md  CHANGELOG.md
```

## Requirements

- **Node.js** 22.13.0+ within 22.x, or 24.x, or 26.x (from the upstream README).
- **Python 3.10+** for scans, policy generation, exports, scan history, and saved findings
  (Python 3.10 also needs `tomli`).
- Node 22.15.0+ if you need to read compressed `.jsonl.zst` session logs.
- An OpenAI account for `cs login` — no credentials are stored in this repository.

## Install and verify

```bash
npm install --global @openai/codex-security@0.6.0
cs --version          # verified: 0.6.0
# `cs` and `codex-security` are the same CLI
```

The smoke test runs this binary directly from `node_modules/.bin`:

```
PASS  codex-security v0.6.0
```

## CLI workflow

```bash
cs login                     # authenticate
cs policy .                  # draft SECURITY.md (saved OUTSIDE your repo for review)
cs scan .                    # scan the current repository
cs scan . --path src --path tests      # narrow scope
cs scan . --diff origin/main --json    # scan only changed code
cs scan . --dry-run                    # validate config without scanning
cs scan . --fail-on-severity high      # exit 1 on high/critical findings
```

Exit codes: `0` = report produced, `1` = findings at/above the configured severity,
`2` = execution or required-output failure. **Scans are report-only by default**, so CI
stays green unless you opt in with `--fail-on-severity`.

## TypeScript SDK

```ts
import { CodexSecurity } from '@openai/codex-security';

const security = new CodexSecurity();
try {
  const result = await security.run('/path/to/repository');

  console.log(result.reportPath);
  console.log(result.findings.findings);

  if (result.hasFindingsAtOrAbove('high')) {
    process.exitCode = 1;   // the SDK never sets exit status for you
  }
} finally {
  await security.close();
}
```

Result shape (per upstream docs): `findings`, `repositoryFindings`, `reportPath`,
`sarifPath`, `coverage`, `threatModel`/`threatModelPath`, `cost`. Scan options go in the
second argument to `run()` (`target`, output dir, deep-scan settings, investigation time,
cost limits, …).

```ts
const result = await security.run('/path/to/repo', {
  target: ['src', 'tests'],
  // outputDir must be a new/empty dir OUTSIDE the scanned repo and its Git worktree
});
```

## CI / GitHub Action

Two supported shapes:

1. **`action.yml`** in the repo root — use it as a workflow step.
2. **Docker** — `compose.yaml` / `compose.runner.yaml` / `compose.apparmor.yaml` for
   containerised bulk scans across many repositories.

Import paths for findings: native scanner output, **SARIF**, GitHub code scanning alerts, and
Snyk Open Source JSON (licence findings and other Snyk products are excluded). Publishing goes
to Linear or a findings service.

## Where to start reading

1. `sdk/typescript/src/` — the `CodexSecurity` client, scan orchestration, validation.
2. `docs/agent-workflows.md` — using it inside a Codex session (choose a task, recover when
   a required tool is unavailable).
3. `docs/cli.md` — full command reference: history, reruns, bulk/component scans, imports,
   patching, integrations.
4. `plugins/codex-security/` — the plugin wiring if you want it in an agent loop, not CI.

## Caveats that matter

- **Reports can contain source code, vulnerability details, and secrets.** Upstream says so
  explicitly; review before sharing. Keep `outputDir` out of the scanned repository.
- **A clean scan is not proof of security.** Read `coverage` alongside findings — it records
  what was actually assessed and where the gaps are.
- Pre-1.0: minor releases may change the public API (currently 0.6.0).
- It needs an authenticated OpenAI account for real scans; the offline check here only
  proves installation and CLI execution.
