.PHONY: help package docs-sync bootstrap bootstrap-fast status verify verify-python verify-node pins pins-write relink clean docs-check rister-demo rister-test rister-scan rister-check

help:
	@echo "Agent frameworks vendoring repo"
	@echo
	@echo "  make bootstrap       full-history clones of all six (~2.3 GB)"
	@echo "  make bootstrap-fast  depth-1 clones (~526 MB)"
	@echo "  make status          pinned vs checked-out SHAs"
	@echo "  make verify          install pinned packages + run all smoke tests"
	@echo "  make pins            report upstream drift (read-only)"
	@echo "  make pins-write      update MANIFEST.json to latest releases"
	@echo "  make relink          restore submodule gitlinks from MANIFEST.json"
	@echo "  make rister-demo     run the RISTER end-to-end demo (offline)"
	@echo "  make rister-test     run the RISTER test suite (173 tests)"
	@echo "  make rister-scan     self-scan RISTER with its own security scanner"
	@echo "  make rister-check    test + demo + scan gate (no network, no deps)"
	@echo "  make package         build rister-<version>.zip and test the archive"
	@echo "  make docs-sync       rewrite the measured numbers in the docs"
	@echo "  make clean           remove .venv and node_modules (keeps vendor/)"

bootstrap:
	./scripts/bootstrap.sh

bootstrap-fast:
	./scripts/bootstrap.sh --shallow

status:
	./scripts/bootstrap.sh --status

verify:
	./scripts/verify.sh

verify-python:
	./scripts/verify.sh --python-only

verify-node:
	./scripts/verify.sh --node-only

pins:
	./scripts/sync-pins.sh

pins-write:
	./scripts/sync-pins.sh --write

rister-demo:
	cd rister && PYTHONPATH=. python3 -m rister demo

rister-test:
	cd rister && PYTHONPATH=. python3 -m rister.tests

rister-scan:
	cd rister && PYTHONPATH=. python3 -m rister scan rister --fail-on-severity high

rister-check: rister-test rister-demo rister-scan
	@echo "RISTER: tests, demo and self-scan all passed"

relink:
	./scripts/relink.sh

# Sanity check: MANIFEST/gitmodules consistency + README doc links.
package:
	python3 scripts/package.py --out . --check --verify

docs-sync:
	python3 scripts/sync_docs.py

docs-check:
	python3 scripts/docs_check.py
	python3 scripts/sync_docs.py --check

clean:
	rm -rf .venv smoke/node/node_modules smoke/node/package-lock.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned (vendor/ untouched — use 'deinit' to drop clones)"
