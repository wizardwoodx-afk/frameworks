#!/usr/bin/env bash
# Compare the pins recorded in MANIFEST.json against each upstream's latest release.
#
#   ./scripts/sync-pins.sh            # report drift (read-only, safe)
#   ./scripts/sync-pins.sh --write    # update MANIFEST.json pins + print the
#                                     # git commands that re-point the submodules
#
# Set GITHUB_TOKEN to avoid unauthenticated API rate limits in CI.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WRITE=0
[ "${1:-}" = "--write" ] && WRITE=1

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }

api_headers=(-H "Accept: application/vnd.github+json")
[ -n "${GITHUB_TOKEN:-}" ] && api_headers+=(-H "Authorization: Bearer $GITHUB_TOKEN")

report=$(python3 - <<'PY'
import json, os, subprocess, sys, urllib.request

manifest = json.load(open("MANIFEST.json"))
token = os.environ.get("GITHUB_TOKEN")
rows = []

def api(path):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Accept": "application/vnd.github+json", **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def sha_for_tag(repo, tag):
    out = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"],
                         capture_output=True, text=True, timeout=60).stdout
    # annotated tags: the ^{} line is the commit; prefer it
    lines = [l.split() for l in out.strip().splitlines() if l.strip()]
    if not lines:
        return ""
    deref = [l[0] for l in lines if l[1].endswith("^{}")]
    return deref[0] if deref else lines[-1][0]

for fw in manifest["frameworks"]:
    repo = fw["repo"]
    try:
        rel = api(f"repos/{repo}/releases/latest")
        latest_tag = rel.get("tag_name", "")
    except Exception as e:  # noqa: BLE001
        rows.append({"id": fw["id"], "pinned": fw["pin"]["tag"], "latest": f"ERROR {e}", "state": "error", "sha": ""})
        continue
    pinned = fw["pin"]["tag"]
    if latest_tag == pinned:
        rows.append({"id": fw["id"], "pinned": pinned, "latest": latest_tag, "state": "up to date", "sha": ""})
    else:
        sha = sha_for_tag(repo, latest_tag)
        rows.append({"id": fw["id"], "pinned": pinned, "latest": latest_tag, "state": "UPDATE", "sha": sha})

print(json.dumps(rows))
PY
) || { echo "resolution failed" >&2; exit 1; }

echo "$report" | python3 -c '
import json,sys
rows=json.load(sys.stdin)
w=max(len(r["id"]) for r in rows)
print(f"{\"REPO\":<{w}}  {\"PINNED\":<28} {\"LATEST\":<28} STATE")
print("-"*(w+70))
for r in rows:
    print(f"{r[\"id\"]:<{w}}  {r[\"pinned\"]:<28} {r[\"latest\"]:<28} {r[\"state\"]}")
' 2>/dev/null || echo "$report" | python3 -m json.tool

if [ "$WRITE" = "1" ]; then
  echo
  echo "$report" | python3 -c '
import json, subprocess, sys
rows = json.load(sys.stdin)
updates = [r for r in rows if r["state"] == "UPDATE" and r["sha"]]
if not updates:
    print("Nothing to update."); sys.exit(0)
subprocess.run([sys.executable, "scripts/_apply_pins.py", json.dumps(updates)], check=True)
for r in updates:
    print(f"git update-index --add --cacheinfo 160000,{r[\"sha\"]},vendor/{r[\"id\"]}")
print("\nMANIFEST.json updated. Re-run ./scripts/bootstrap.sh --status after re-pointing.")
'
fi
