#!/usr/bin/env python3
"""Helper for scripts/sync-pins.sh --write: apply tag/sha updates to MANIFEST.json.

Takes a JSON array of {id, pinned, latest, sha} on argv[1] and rewrites the
matching framework entries. Kept separate so sync-pins.sh stays readable.
"""
from __future__ import annotations

import json
import subprocess
import sys


def commit_date(repo: str, sha: str) -> str:
    import os
    import urllib.request

    token = os.environ.get("GITHUB_TOKEN")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{sha}",
        headers={
            "Accept": "application/vnd.github+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["commit"]["committer"]["date"]
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    updates = {u["id"]: u for u in json.loads(sys.argv[1])}
    with open("MANIFEST.json") as fh:
        manifest = json.load(fh)

    for fw in manifest["frameworks"]:
        upd = updates.get(fw["id"])
        if not upd:
            continue
        fw["pin"]["tag"] = upd["latest"]
        fw["pin"]["sha"] = upd["sha"]
        date = commit_date(fw["repo"], upd["sha"])
        if date:
            fw["pin"]["committed"] = date
        pkg = fw["packages"][0]
        if pkg["ecosystem"] in {"pypi", "npm"}:
            # package versions usually drop a leading "v" / package-name prefix
            version = upd["latest"].split("@")[-1].lstrip("v")
            if pkg["ecosystem"] == "pypi" and "_v" in version:
                version = version.split("_v")[-1]
            pkg["version"] = version

    manifest["generated"] = subprocess.run(
        ["date", "-u", "+%Y-%m-%d"], capture_output=True, text=True
    ).stdout.strip()

    with open("MANIFEST.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
