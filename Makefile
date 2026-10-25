.PHONY: help bootstrap bootstrap-fast status verify verify-python verify-node pins pins-write clean docs-check

help:
	@echo "Agent frameworks vendoring repo"
	@echo
	@echo "  make bootstrap       full-history clones of all six (~2.3 GB)"
	@echo "  make bootstrap-fast  depth-1 clones (~526 MB)"
	@echo "  make status          pinned vs checked-out SHAs"
	@echo "  make verify          install pinned packages + run all smoke tests"
	@echo "  make pins            report upstream drift (read-only)"
	@echo "  make pins-write      update MANIFEST.json to latest releases"
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

# Sanity check: manifest parses, pins match .gitmodules, docs links resolve.
docs-check:
	@python3 - <<'PY'
	import json, os, re, sys
	ok = True
	m = json.load(open("MANIFEST.json"))
	mods = open(".gitmodules").read()
	for fw in m["frameworks"]:
	    path = fw["path"]
	    if f"path = {path}" not in mods:
	        print(f"FAIL {fw['id']}: {path} missing from .gitmodules"); ok = False
	    if not (len(fw["pin"]["sha"]) == 40):
	        print(f"FAIL {fw['id']}: sha is not 40 chars"); ok = False
	    if not os.path.isdir(path):
	        print(f"WARN {fw['id']}: {path} not materialised (run make bootstrap)")
	for doc in re.findall(r"\]\((docs/[0-9a-z\-]+\.md)\)", open("README.md").read()):
	    if not os.path.exists(doc):
	        print(f"FAIL README links to missing {doc}"); ok = False
	print("docs-check:", "OK" if ok else "FAILED")
	sys.exit(0 if ok else 1)
	PY

clean:
	rm -rf .venv smoke/node/node_modules smoke/node/package-lock.json
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned (vendor/ untouched — use 'deinit' to drop clones)"
