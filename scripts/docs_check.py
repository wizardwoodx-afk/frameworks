#!/usr/bin/env python3
"""Consistency check for this repository's metadata and docs.

Verifies that:
  * MANIFEST.json parses and every framework has a 40-char pinned SHA;
  * every manifest path has a matching `path =` entry in .gitmodules;
  * every framework URL appears in .gitmodules;
  * README links to docs/*.md files that actually exist;
  * the vendor/ tree state is reported (materialised vs not) — informational only.

Exit code: 0 = consistent, 1 = a real inconsistency was found.
Missing vendor/ clones are NOT failures: they are expected before `make bootstrap`.
"""
from __future__ import annotations

import json
import os
import re
import sys


def main() -> int:
    ok = True

    try:
        manifest = json.load(open("MANIFEST.json"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL MANIFEST.json does not parse: {exc}")
        return 1

    try:
        modules = open(".gitmodules").read()
    except FileNotFoundError:
        print("FAIL .gitmodules missing")
        return 1

    frameworks = manifest.get("frameworks", [])
    if len(frameworks) != 6:
        print(f"FAIL expected 6 frameworks in MANIFEST.json, found {len(frameworks)}")
        ok = False

    for fw in frameworks:
        fid = fw.get("id", "?")
        path = fw.get("path", "")
        sha = fw.get("pin", {}).get("sha", "")

        if len(sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            print(f"FAIL {fid}: pin.sha is not a 40-char hex commit ({sha!r})")
            ok = False

        if f"path = {path}" not in modules:
            print(f"FAIL {fid}: {path} missing from .gitmodules")
            ok = False

        url = fw.get("url", "")
        if url and f"url = {url}" not in modules:
            print(f"FAIL {fid}: {url} missing from .gitmodules")
            ok = False

        if fw.get("id") and fw.get("pin", {}).get("tag") == "":
            print(f"FAIL {fid}: empty pin tag")
            ok = False

        if not os.path.isdir(path):
            print(f"WARN {fid}: {path} not materialised (run ./scripts/bootstrap.sh)")
        elif not (os.path.exists(os.path.join(path, ".git"))):
            print(f"WARN {fid}: {path} exists but is not a git checkout")
        else:
            print(f"OK   {fid}: {path} @ {sha[:12]}")

    # README links must resolve
    try:
        readme = open("README.md").read()
    except FileNotFoundError:
        print("FAIL README.md missing")
        ok = False
        readme = ""
    for target in re.findall(r"\]\((docs/[0-9a-z\-]+\.md)\)", readme):
        if not os.path.exists(target):
            print(f"FAIL README links to missing {target}")
            ok = False
    for target in re.findall(r"\]\((THIRD-PARTY-NOTICES\.md|MANIFEST\.json)\)", readme):
        if not os.path.exists(target):
            print(f"FAIL README links to missing {target}")
            ok = False

    # every doc should be linked from the README
    docs = sorted(d for d in os.listdir("docs") if d.endswith(".md"))
    for doc in docs:
        if f"docs/{doc}" not in readme:
            print(f"WARN docs/{doc} is not linked from README.md")

    print()
    print("docs-check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
