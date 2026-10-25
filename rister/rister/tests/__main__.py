"""Test runner that needs no third-party packages.

    python -m rister.tests            # run everything
    python -m rister.tests -v         # verbose
    python -m rister.tests state      # only tests/test_state.py

The test functions are plain ``test_*`` callables, so ``pytest rister/tests``
works too if pytest is installed.
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

MODULES = [
    "rister.tests.test_state",
    "rister.tests.test_graph",
    "rister.tests.test_agent",
    "rister.tests.test_govern",
    "rister.tests.test_security_observe_evals",
    "rister.tests.test_debranding",
]


def discover(selectors: list[str]) -> list:
    names = [m for m in MODULES if not selectors or any(s in m for s in selectors)]
    modules = []
    for name in names:
        try:
            modules.append(importlib.import_module(name))
        except ImportError as exc:  # pragma: no cover
            print(f"could not import {name}: {exc}")
    return modules


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    selectors = [a for a in argv if not a.startswith("-")]

    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for module in discover(selectors):
        tests = sorted(
            (name, fn)
            for name, fn in vars(module).items()
            if name.startswith("test_") and callable(fn)
        )
        print(f"\n{module.__name__}  ({len(tests)} tests)")
        for name, fn in tests:
            try:
                fn()
                passed += 1
                if verbose:
                    print(f"  PASS  {name}")
            except Exception:  # noqa: BLE001 - report and continue
                failed += 1
                failures.append((f"{module.__name__}.{name}", traceback.format_exc()))
                print(f"  FAIL  {name}")

    for label, tb in failures:
        print("\n" + "=" * 70)
        print(f"FAILED: {label}")
        print(tb)
    total = passed + failed
    print("\n" + "=" * 70)
    print(f"{passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
