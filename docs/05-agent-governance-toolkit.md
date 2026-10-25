# 5. Microsoft Agent Governance Toolkit (AGT)

> Policy enforcement, zero-trust identity, execution sandboxing, and reliability
> engineering for autonomous AI agents. "One `pip install`, any framework."

- **Pinned:** tag `v4.1.0` → commit `0de71ca6c95cf8b9b975ac96f48eaa7826bbe258` (2026-06-09)
- **Upstream:** https://github.com/microsoft/agent-governance-toolkit
- **Docs:** https://microsoft.github.io/agent-governance-toolkit
- **License:** MIT · © Microsoft Corporation
- **Status:** ⚠ **Public Preview** — upstream warns of breaking changes before GA.
- **Submodule path:** `vendor/agent-governance-toolkit`

## What it is

AGT is **not** an agent framework. It is the control plane around one. The premise, stated
plainly in its README: prompt-level safety is "a polite request to a stochastic system", so
enforcement happens in **deterministic application code before the model's intent reaches
the wire**. Every tool call, message send, and delegation is intercepted and evaluated
against policy.

It answers three operational questions:

1. **Is this action allowed?** — policy enforcement on tool invocation (an agent with
   `send_email` and `query_database` must not be able to `drop_table`).
2. **Which agent did this?** — zero-trust agent identity, so five agents sharing one API key
   is no longer an unattributable incident.
3. **Can you prove what happened?** — tamper-evident audit records of the active policy,
   the request, and the allow/deny rationale.

## Layout at the pinned commit

Root has per-language implementations; Python is the deepest:

```
vendor/agent-governance-toolkit/
├── agent-governance-python/          # ~20 component packages
│   ├── agent-governance-toolkit-core/        # kernel primitives
│   ├── agent-governance-toolkit-protocols/   # protocol definitions
│   ├── agent-governance-toolkit-integrations/# framework hooks
│   ├── agent-governance-toolkit-cli/
│   ├── agent-os/            # governance kernel (StatelessKernel, ExecutionContext)
│   ├── agent-runtime/       # runtime supervisor w/ execution rings
│   ├── agent-mesh/          # zero-trust agent comms ("SSL for AI agents")
│   ├── agent-sandbox/  agent-hypervisor/
│   ├── agent-sre/           # SRE for agents
│   ├── agent-compliance/    # ← the runtime module PyPI installs as agent_compliance
│   ├── agent-mcp-governance/ agent-rag-governance/
│   ├── agent-marketplace/  agent-discovery/  agent-learning/  agent-lightning/
│   ├── agent-primitives/  agentmesh-integrations/  agt-policies/
│   └── benchmarks/ fuzz/ notebooks/ requirements/
├── agent-governance-typescript/      # @microsoft/agent-governance-sdk
├── agent-governance-dotnet/          # Microsoft.AgentGovernance
├── agent-governance-golang/  agent-governance-rust/
├── agent-governance-claude-code/ agent-governance-copilot-cli/
├── agent-governance-antigravity-cli/ agent-governance-opencode/
├── policy-engine/  action/  examples/  docs/  tests/
└── mkdocs.yml  Dockerfile  docker-compose.yml  VERSION
```

> Note: `agent-governance-codex-cli/` and `schemas/` exist on `main` but **not** at `v4.1.0`.
> That is exactly why this repo pins a commit instead of tracking a branch.

## Install

```bash
pip install agent-governance-toolkit==4.1.0        # unified installer
pip install "agent-governance-toolkit[full]"       # heavier component set
```

The PyPI package installs the module **`agent_compliance`** (version `3.2.2` at this pin) and
four CLI entry points: `agt`, `agent-governance`, `agent-governance-toolkit`,
`agent-compliance`.

## Verified — the CLI

```
$ agt --version
agt, version 4.1.0

$ agent-governance --help
usage: agent-compliance [-h] {verify,integrity,lint-policy} ...
  verify        Run OWASP ASI 2026 governance verification
  integrity     Verify or generate module integrity manifest
  lint-policy   Lint YAML policy files for common mistakes
```

Useful in CI: `lint-policy` on your policy YAML, `integrity` to check module manifests,
`verify` to produce an OWASP-aligned governance report.

## Verified — prompt defence (be precise about what it does)

`PromptDefenseEvaluator` is a **static analyser of a system prompt**, not a runtime
classifier of user input. It scores which of **12 OWASP-aligned defensive instructions are
present** and lists the missing ones. Deterministic, no LLM, no network.

```python
from agent_compliance import PromptDefenseConfig, PromptDefenseEvaluator

ev = PromptDefenseEvaluator(PromptDefenseConfig())
report = ev.evaluate("You are a helpful assistant.")

print(report.grade)    # 'F'
print(report.score)    # 8
print(report.missing)  # 11 vectors, incl. 'instruction-override', 'data-leakage', ...
print(report.total)    # 12
print(report.is_blocking())
```

Hardening the prompt measurably improves the grade (verified: `F/8` → `D/42`), which is the
intended workflow — audit your prompts, close the gaps, re-run.

## Verified — supply-chain guard

Catches unpinned dependencies and typosquats in a `requirements.txt`:

```python
from agent_compliance import SupplyChainConfig, SupplyChainGuard

guard = SupplyChainGuard(SupplyChainConfig())
for f in guard.check_requirements("requirements.txt"):
    print(f.package, f.version, f.severity, f.rule, f.message)
```

Given a file containing `requests==2.31.0`, `flask>=2.0`, `reqeusts==1.0.0`, this reported
**2 findings**: `flask` (`>=2.0`, high — constraint not pinned) and `reqeusts`
(`*`, critical — typosquat). `SupplyChainConfig` also exposes `scan_exclude_dirs` (skips
`node_modules`, `.venv`, `dist`, … so vendored trees don't generate noise), plus `freshness_days`,
`allow_ranges`, and `typosquat_threshold`.

## The runtime interceptor model

The part you integrate is the kernel: you wrap tool execution so every call passes through
policy evaluation and lands in the audit chain. The primitives live in
`agent-os/` (`StatelessKernel`, `ExecutionContext`) and are re-exported opportunistically by
`agent_compliance` — they are **optional** imports, so a minimal install won't have them:

```python
import agent_compliance as ac
ac.PromptDefenseEvaluator      # always present
ac.StatelessKernel             # only with the agent-os component installed
```

Check availability rather than assuming; the package logs a DEBUG line naming the missing
symbol when an optional companion is absent.

## Where to start reading

1. `agent-governance-python/agent-compliance/` — the `PromptDefenseEvaluator`,
   `SupplyChainGuard`, policy linting, and integrity verification you can run today.
2. `agent-os/` — kernel + `ExecutionContext`: the actual enforcement point.
3. `agent-mesh/` — agent identity and zero-trust messaging.
4. `docs/compliance/` — OWASP Agentic Top 10 mapping.
5. `examples/` — framework wiring you can copy.

## Caveats that matter

- **Public Preview.** Pin the version (as done here); expect breaking changes.
- The OWASP coverage claim is nuanced upstream (badge text at `main` reads "7 Full, 3
  Partial" while the repo description says "10/10"). Treat any coverage number as a
  *mapping exercise you must verify against your own threat model*, not a certification.
- `agent_compliance.__version__` (`3.2.2`) differs from the distribution version (`4.1.0`) —
  both are correct; the module is versioned independently of the installer.
- Scope: AGT does not sandbox the model, and it is not a scanner. Pair it with
  [Codex Security](06-codex-security.md) for the static/agentic code side.
