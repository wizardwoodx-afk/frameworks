"""Test runner that needs no third-party packages.

    python -m rister.tests            # run everything
    python -m rister.tests -v         # verbose
    python -m rister.tests state      # only tests/test_state*.py

The test functions are plain ``test_*`` callables, so ``pytest rister/tests``
works too if pytest is installed.
"""
from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

#: Discovered from the package itself, never hand-written: a hardcoded list is a
#: silent-skip waiting to happen. Adding ``test_foo.py`` is enough to run it.
MODULES = sorted(
    f"rister.tests.{path.stem}"
    for path in Path(__file__).resolve().parent.glob("test_*.py")
)


def discover(selectors: list[str]) -> list:
    names = [m for m in MODULES if not selectors or any(s in m for s in selectors)]
    if not names:
        raise SystemExit(f"no test module matches {selectors!r}")
    # An unimportable test module is a failure, not a note: skipping it silently
    # is how a suite reports green while covering nothing.
    return [importlib.import_module(name) for name in names]


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
