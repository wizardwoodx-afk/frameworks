#!/usr/bin/env node
/**
 * Functional smoke test for the three TypeScript/npm packages vendored here.
 *
 *   * Mastra      : imports @mastra/core and constructs a Mastra instance.
 *   * VoltAgent   : imports @voltagent/core and resolves the Agent class + tool surface.
 *   * Codex Security: executes the real `codex-security` CLI binary from node_modules/.bin.
 *
 * No API keys, no model calls, no network required (VoltAgent may log a harmless
 * "registry refresh failed" warning when offline - it does not fail this test).
 *
 * Usage: node smoke.mjs [--json]
 * Exit : 0 = all checks passed, 1 = at least one FAIL
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const RESULTS = [];
const JSON_OUT = process.argv.includes('--json');

function pkgVersion(name) {
  // Read from disk rather than require.resolve(): @voltagent/core and
  // @openai/codex-security do not expose "./package.json" through their
  // "exports" maps, which makes require.resolve() throw ERR_PACKAGE_PATH_NOT_EXPORTED.
  const file = join(HERE, 'node_modules', ...name.split('/'), 'package.json');
  if (!existsSync(file)) throw new Error(`${name} not installed (looked in ${file})`);
  return JSON.parse(readFileSync(file, 'utf8')).version;
}

/** Run fn with the known "no storage adapter" Mastra warning muted. */
function muteExpectedWarnings(fn) {
  const original = console.warn;
  console.warn = (...args) => {
    const text = args.map(String).join(' ');
    if (text.includes('No `storage` configured on Mastra')) return;
    original(...args);
  };
  try {
    return fn();
  } finally {
    console.warn = original;
  }
}

async function check(suite, name, fn) {
  try {
    const detail = await fn();
    RESULTS.push({ suite, check: name, status: 'PASS', detail: String(detail ?? '') });
  } catch (err) {
    RESULTS.push({ suite, check: name, status: 'FAIL', detail: `${err.name}: ${err.message}` });
  }
}

// --------------------------------------------------------------------------
// Mastra
// --------------------------------------------------------------------------
await check('mastra', 'distribution version', () => `@mastra/core ${pkgVersion('@mastra/core')}`);

await check('mastra', 'Mastra instantiates', async () => {
  const { Mastra } = await import('@mastra/core');
  if (typeof Mastra !== 'function') throw new Error('Mastra export is not a constructor');
  // In-memory fallback is expected here (no storage adapter configured).
  const mastra = muteExpectedWarnings(() => new Mastra({}));
  if (typeof mastra.getAgent !== 'function') throw new Error('instance lacks getAgent()');
  return `new Mastra({}) ok; getAgent present`;
});

await check('mastra', 'component registry reachable', async () => {
  const { Mastra } = await import('@mastra/core');
  const mastra = muteExpectedWarnings(() => new Mastra({}));
  // These accessors back agents/workflows/tools/vectors throughout the framework.
  const methods = ['getAgent', 'getWorkflow', 'getTool', 'getVector'].filter(
    (m) => typeof mastra[m] === 'function',
  );
  if (methods.length < 3) throw new Error(`unexpected Mastra surface: ${methods.join(',')}`);
  return methods.join(', ');
});

// --------------------------------------------------------------------------
// VoltAgent
// --------------------------------------------------------------------------
await check('voltagent', 'distribution version', () => `@voltagent/core ${pkgVersion('@voltagent/core')}`);

await check('voltagent', 'Agent class + runtime exports', async () => {
  const v = await import('@voltagent/core');
  if (typeof v.Agent !== 'function') throw new Error('Agent export missing');
  const surface = ['Agent', 'AgentRegistry', 'BaseRetriever'].filter((k) => k in v);
  if (surface.length < 3) throw new Error(`missing expected exports: ${surface.join(',')}`);
  return `exports ${Object.keys(v).length} symbols (${surface.join(', ')} ...)`;
});

await check('voltagent', 'Agent instance constructs', async () => {
  const { Agent } = await import('@voltagent/core');
  // A throwaway model stub keeps this offline: we only assert construction wiring.
  const agent = new Agent({
    name: 'smoke-agent',
    instructions: 'You are a smoke test.',
    model: { provider: 'openai', modelId: 'gpt-4o-mini', toolChoice: 'auto' },
  });
  if (agent.name !== 'smoke-agent') throw new Error(`name mismatch: ${agent.name}`);
  return `Agent("${agent.name}") constructed`;
});

// --------------------------------------------------------------------------
// Codex Security
// --------------------------------------------------------------------------
await check('codex-security', 'distribution version', () => {
  return `@openai/codex-security ${pkgVersion('@openai/codex-security')}`;
});

await check('codex-security', 'CLI binary executes', () => {
  const bin = join(HERE, 'node_modules', '.bin', 'codex-security');
  if (!existsSync(bin)) throw new Error(`CLI not found at ${bin} (run npm install)`);
  const out = execFileSync(bin, ['--version'], { encoding: 'utf8', timeout: 120_000 }).trim();
  if (!/^\d+\.\d+\.\d+/.test(out)) throw new Error(`unexpected --version output: ${out}`);
  return `codex-security v${out.split('\n')[0]}`;
});

// --------------------------------------------------------------------------
const failed = RESULTS.filter((r) => r.status === 'FAIL');

if (JSON_OUT) {
  console.log(JSON.stringify(RESULTS, null, 2));
} else {
  let current = null;
  let width = Math.max(...RESULTS.map((r) => r.check.length));
  for (const r of RESULTS) {
    if (r.suite !== current) {
      current = r.suite;
      console.log(`\n\x1b[1m${r.suite}\x1b[0m`);
    }
    const colour = r.status === 'PASS' ? '\x1b[32m' : '\x1b[31m';
    console.log(`  ${colour}${r.status}\x1b[0m  ${r.check.padEnd(width)}  ${r.detail}`);
  }
  console.log(`\n${RESULTS.length - failed.length} passed, ${failed.length} failed`);
}

process.exit(failed.length ? 1 : 0);
