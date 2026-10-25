#!/usr/bin/env bash
# Install the pinned packages and prove all six frameworks load and execute.
#
#   ./scripts/verify.sh                  # full: python + node smoke tests
#   ./scripts/verify.sh --python-only
#   ./scripts/verify.sh --node-only
#   ./scripts/verify.sh --skip-install   # run against whatever is already installed
#
# Requirements: python3 (>=3.11 for BeeAI), node >=22.13 (for codex-security),
# npm, git. Submodules are NOT required for this script - it tests the
# published packages, which is what you actually import.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DO_PY=1; DO_NODE=1; DO_INSTALL=1; VENV="$ROOT/.venv"

while [ $# -gt 0 ]; do
  case "$1" in
    --python-only) DO_NODE=0 ;;
    --node-only)   DO_PY=0 ;;
    --skip-install) DO_INSTALL=0 ;;
    --venv) VENV="${2:?--venv needs a path}"; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

FAIL=0
banner() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

if [ "$DO_PY" = "1" ]; then
  banner "Python: langgraph + beeai-framework + agent-governance-toolkit"
  PY="$VENV/bin/python"
  if [ ! -x "$PY" ]; then
    echo "creating venv at $VENV"
    python3 -m venv "$VENV" || { echo "venv creation failed"; exit 1; }
  fi
  if [ "$DO_INSTALL" = "1" ]; then
    "$VENV/bin/pip" -q install --upgrade pip >/dev/null 2>&1 || true
    echo "installing pinned wheels (this takes ~30s on first run)..."
    if ! "$VENV/bin/pip" -q install -r smoke/python/requirements.txt; then
      echo "pip install FAILED"; exit 1
    fi
  fi
  "$PY" smoke/python/smoke_test.py || FAIL=1
fi

if [ "$DO_NODE" = "1" ]; then
  banner "Node: @mastra/core + @voltagent/core + @openai/codex-security"
  if ! command -v node >/dev/null; then
    echo "node not found - skipping"; [ "$DO_PY" = "0" ] && FAIL=1
  else
    if [ "$DO_INSTALL" = "1" ]; then
      echo "installing pinned npm packages..."
      ( cd smoke/node && npm install --no-audit --no-fund --silent ) || { echo "npm install FAILED"; exit 1; }
    fi
    ( cd smoke/node && node smoke.mjs ) || FAIL=1
  fi
fi

banner "verify.sh finished"
if [ "$FAIL" = "0" ]; then
  printf '\033[1;32mALL CHECKS PASSED\033[0m\n'; exit 0
else
  printf '\033[1;31mSOME CHECKS FAILED\033[0m\n'; exit 1
fi
