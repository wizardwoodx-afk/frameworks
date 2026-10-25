# Verification log

Everything below was executed in this workspace on **2026-10-25**. It records what was run,
what it printed, and the API assumptions that turned out to be **wrong** and had to be fixed.
No check in this document relies on an API key, a model call, or network access at test time.

## Environment

| | |
|---|---|
| OS | Linux (Debian) |
| Python | 3.11.2 |
| Node | v22.22.3 |
| npm | 10.9.8 |

Python 3.11 is the practical floor here: BeeAI requires `>=3.11,<3.14`. Codex Security
requires Node `>=22.13`; v22.22.3 satisfies it.

---

## 1. Resolving the pins

Latest stable release per upstream, resolved via the GitHub API, then the tag dereferenced to
a commit SHA (`^{}` for annotated tags) so the pin is immutable:

| Framework | Release tag | Commit | Commit date |
|---|---|---|---|
| LangGraph | `1.2.17` | `4c52e129eb1a` | 2026-10-24 |
| Mastra | `@mastra/core@1.79.0` | `4c22623ea342` | 2026-10-21 |
| BeeAI | `python_v0.1.85` | `0304ca17c975` | 2026-09-28 |
| VoltAgent | `@voltagent/core@2.11.0` | `72a46c76b450` | 2026-09-28 |
| AGT | `v4.1.0` | `0de71ca6c95c` | 2026-06-09 |
| Codex Security | `npm-v0.6.0` | `13dcc6fec00a` | 2026-10-22 |

Cross-checks performed:

- AGT's GitHub **`releases/latest`** is `v4.1.0`, even though a `v5.0.0` tag exists — `v5.0.0`
  is not a published release. The PyPI latest (`4.1.0`) agrees with the release, so `v4.1.0` was pinned.
- Package registries agree with the tags: PyPI `langgraph 1.2.17`, `beeai-framework 0.1.85`,
  `agent-governance-toolkit 4.1.0`; npm `@mastra/core@1.79.0`, `@voltagent/core@2.11.0`,
  `@openai/codex-security@0.6.0` (all `latest` dist-tags, all with resolvable tarballs).
- **Tag ≠ branch HEAD** for four of six. `openai/codex-security` `main` was already ahead of
  `npm-v0.6.0`; AGT `main` contains directories (`agent-governance-codex-cli/`, `schemas/`)
  that do **not** exist at `v4.1.0`. Pinning commits — not branches — is what keeps this
  reproducible.

## 2. Size measurement (why submodules)

Depth-1 clones of all six, measured with `du -sh`:

```
langgraph                  20M    698 files    (487 source)
mastra                    348M  21588 files  (17078 source)
beeai-framework            12M   1196 files   (1023 source)
voltagent                  34M   2883 files   (1663 source)
agent-governance-toolkit   84M   4982 files   (2440 source)
codex-security             28M   1219 files    (838 source)
                          --------           -----------
                          526M  32566 files  (23529 source)
```

Full histories are ~2.3 GB (GitHub reports Mastra alone at ~1.6 GB packed). That is far past
what belongs in a single Git repository, so the sources are gitlinks and
`scripts/bootstrap.sh` materialises them.

## 3. Python: install + execute

```bash
python3 -m venv /tmp/v
/tmp/v/bin/pip install langgraph==1.2.17 beeai-framework==0.1.85 agent-governance-toolkit==4.1.0
```

Installed versions:

```
langgraph                      1.2.17
beeai-framework                0.1.85
agent-governance-toolkit       4.1.0
langchain-core                 1.7.0     (transitive)
agent_compliance (module)      3.2.2     (inside the 4.1.0 distribution)
```

Then `python smoke/python/smoke_test.py`:

```
langgraph
  PASS  distribution version        langgraph 1.2.17
  PASS  StateGraph executes         START->inc->dbl->END produced n=12
  PASS  checkpointer round-trip     thread snapshot restored n=2 (next=())
  PASS  conditional routing         conditional edge routed 80->passed, 10->failed
beeai
  PASS  distribution version        beeai-framework 0.1.85
  PASS  Workflow executes           async workflow (3+1)*2 = 8
  PASS  agent/tool surface          ReActAgent, ChatModel, tool='OpenMeteoTool'
agt
  PASS  distribution version        agent-governance-toolkit 4.1.0
  PASS  governance runtime imports  agent_compliance 3.2.2 exposes PromptDefenseEvaluator, SupplyChainGuard
  PASS  prompt-defence executes     12 vectors; weak=F/8 -> hardened=D/42
  PASS  agt CLI runs                agt, version 4.1.0

11 passed, 0 failed, 0 skipped
```

Independent CLI check:

```
$ agt --version
agt, version 4.1.0

$ agent-governance --help
usage: agent-compliance [-h] {verify,integrity,lint-policy} ...
  verify        Run OWASP ASI 2026 governance verification
  integrity     Verify or generate module integrity manifest
  lint-policy   Lint YAML policy files for common mistakes
```

Supply-chain guard, run against a file containing `requests==2.31.0`, `flask>=2.0`,
`reqeusts==1.0.0`, produced 2 findings: `flask` (high, range not pinned) and `reqeusts`
(critical, typosquat).

## 4. Node: install + execute

```bash
npm install @mastra/core@1.79.0 @voltagent/core@2.11.0 @openai/codex-security@0.6.0
# -> @mastra/core 1.79.0, @voltagent/core 2.11.0, @openai/codex-security 0.6.0
```

Then `node smoke/smoke.mjs`:

```
mastra
  PASS  distribution version           @mastra/core 1.79.0
  PASS  Mastra instantiates            new Mastra({}) ok; getAgent present
  PASS  component registry reachable   getAgent, getWorkflow, getTool, getVector
voltagent
  PASS  distribution version           @voltagent/core 2.11.0
  PASS  Agent class + runtime exports  exports 202 symbols (Agent, AgentRegistry, BaseRetriever ...)
  PASS  Agent instance constructs      Agent("smoke-agent") constructed
codex-security
  PASS  distribution version           @openai/codex-security 0.6.0
  PASS  CLI binary executes            codex-security v0.6.0

8 passed, 0 failed
```

Two additional checks were run outside the smoke suite, because they are the most
misunderstood APIs in the repo:

```js
// Mastra workflow: .then(...) MUST be followed by .commit()
const flow = createWorkflow({ id: 'demo-flow' }).then(doubleStep).commit();
const run  = await flow.createRun();
const out  = await run.start({ inputData: { value: 21 } });
// -> out.status === 'success', out.result === { value: 42 }
```

```
$ node_modules/.bin/codex-security --version
0.6.0
```

## 5. Assumptions that were wrong (and corrected)

These are the interesting part of the log — each one would have shipped as a broken example.

| # | Assumption | Reality | Fix |
|---|---|---|---|
| 1 | BeeAI weather tool is `OpenMeteoWeatherTool` | Class is **`OpenMeteoTool`** (`tools/weather/openmeteo.py`) | corrected import in docs + smoke test |
| 2 | BeeAI `Workflow` accepts a dataclass state | Requires a **Pydantic `BaseModel`**; dataclass raises `ValidationError` inside `WorkflowRun` | switched to `BaseModel` |
| 3 | `asyncio.run(wf.run(state))` works | `wf.run()` returns an awaitable **`Run`** → `ValueError: a coroutine was expected` | `out = await wf.run(state)` |
| 4 | `PromptDefenseEvaluator` detects prompt injection at runtime | It is a **static analyser of the system prompt**: 12 defence vectors, grades F→A, lists missing defences | rewrote the test to assert `weak=F/8` → `hardened=D/42` |
| 5 | `Agent` is exported from `@mastra/core` | Default surface exports **only `Mastra`** (and related); `Agent` lives at **`@mastra/core/agent`** | corrected import path |
| 6 | `createWorkflow(...).then(step)` is runnable | Throws `Uncommitted step flow changes detected. Call .commit()` | added `.commit()` |
| 7 | `require.resolve('<pkg>/package.json')` works everywhere | `@voltagent/core` and `@openai/codex-security` don't export `./package.json` (`ERR_PACKAGE_PATH_NOT_EXPORTED`) | read the file from disk instead |
| 8 | `agt` is the only CLI | Four entry points: `agt`, `agent-governance`, `agent-governance-toolkit`, `agent-compliance` | documented all four |
| 9 | AGT repo layout at `v4.1.0` matches `main` | `agent-governance-codex-cli/` and `schemas/` exist only on `main` | documented against the **pinned tree** |
| 10 | `shutil.which("agt")` finds the CLI | The entry point lives in `.venv/bin`, which may not be on `PATH` | resolve next to `sys.executable` |
| 11 | Clone URL can be derived from the local name (`https://github.com/<name>.git`) | Slugs differ: `i-am-bee/beeai-framework`, `VoltAgent/voltagent`, `microsoft/agent-governance-toolkit`, `openai/codex-security`, `langchain-ai/langgraph`, `mastra-ai/mastra` | `bootstrap.sh` now reads `url` from `MANIFEST.json` |
| 12 | Gitlinks survive `git add -A` | They do **once initialised**; before that, empty dirs make `git add -A` stage deletions | added `scripts/relink.sh` / `make relink` + a README troubleshooting note |

## 6. Expected noise (not failures)

- **Mastra**, no storage adapter configured:
  `No 'storage' configured on Mastra — falling back to an in-memory store.`
  Correct behaviour for `new Mastra({})`; the test filters it from the table and the docs say
  to configure `@mastra/libsql`/`@mastra/pg` for durability.
- **VoltAgent**, offline:
  `[ModelProviderRegistry] Auto-refresh failed: TypeError: fetch failed ... host: 'models.dev'`
  Importing `@voltagent/core` refreshes a model registry in the background. The sandbox
  blocks the host; module loading and agent construction are unaffected.

## 7. End-to-end test in a clean clone

The tooling was validated the way a new user would experience it — `git clone` this
repository into an empty directory, then run the scripts (not the working copy):

```bash
git clone <this-repo> /tmp/testc && cd /tmp/testc

./scripts/bootstrap.sh --shallow --only beeai-framework
#   [beeai-framework] ok (0304ca17c975)   9.1M
#   -> git submodule status: 0304ca17c975... vendor/beeai-framework (python_v0.1.85)
#   -> vendor/beeai-framework/.git = "gitdir: ../../.git/modules/vendor/beeai-framework"
#      i.e. it is a genuine submodule, not a plain directory

./scripts/bootstrap.sh --shallow --jobs 2 --only voltagent,langgraph
#   parallel clone path works: langgraph ok (16M), voltagent ok (27M)

./scripts/bootstrap.sh --status
#   langgraph / beeai-framework / voltagent ....... ok
#   mastra / agent-governance-toolkit / codex-security ... not initialised

./scripts/verify.sh
#   11 passed, 0 failed, 0 skipped     (python)
#    8 passed, 0 failed                (node)
#   ALL CHECKS PASSED
```

That run installed the pinned wheels and npm packages from scratch in the clone and
executed all 19 checks — so the flow in the README is the flow that was actually tested.

## 8. Reproducing all of it

```bash
./scripts/bootstrap.sh --shallow   # ~526 MB of upstream source
./scripts/verify.sh                # creates .venv, installs pins, runs both suites
```

`verify.sh` exits non-zero if any check fails, so it is CI-ready as-is
(see `.github/workflows/verify.yml`).
