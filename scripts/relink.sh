#!/usr/bin/env bash
# Restore the six submodule gitlinks in the index from MANIFEST.json.
#
# Why this exists: while the submodules are NOT yet initialised, their directories
# are empty. `git add -A` sees an empty directory where the index expects a gitlink
# and stages a *deletion* of it. Once a submodule is initialised (has .git / an
# entry under .git/modules) git preserves the gitlink normally.
#
# Run this if `git submodule status` or `git status` shows the vendor entries as
# deleted, or after any bulk `git add -A` on a freshly cloned checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import json, subprocess

manifest = json.load(open("MANIFEST.json"))
restored = []
for fw in manifest["frameworks"]:
    path, sha = fw["path"], fw["pin"]["sha"]
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}"],
        check=True,
    )
    restored.append((path, sha, fw["pin"]["tag"]))

for path, sha, tag in restored:
    print(f"relinked {path:<32} {sha[:12]}  ({tag})")
print(f"\n{len(restored)} gitlinks restored.")
PY

echo
echo "Verify with: git ls-files --stage vendor/   (expect 6 lines of mode 160000)"
