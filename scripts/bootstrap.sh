#!/usr/bin/env bash
# Materialise the six vendored upstream repositories as git submodules.
#
#   ./scripts/bootstrap.sh                 # full history clones (default)  ~2.3 GB
#   ./scripts/bootstrap.sh --shallow       # depth-1 clones                 ~526 MB
#   ./scripts/bootstrap.sh --only beeai-framework,voltagent
#   ./scripts/bootstrap.sh --shallow --jobs 6
#   ./scripts/bootstrap.sh --verify        # bootstrap then run smoke tests
#   ./scripts/bootstrap.sh --status        # show pinned vs checked-out SHAs
#
# Re-runnable: already-initialised submodules are updated, not re-cloned.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ALL=(langgraph mastra beeai-framework voltagent agent-governance-toolkit codex-security)
SHALLOW=0; JOBS=1; DO_VERIFY=0; STATUS_ONLY=0; ONLY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --shallow) SHALLOW=1 ;;
    --depth)   SHALLOW=1; [ "${2:-}" != "" ] && shift ;;
    --jobs)    JOBS="${2:?--jobs needs a number}"; shift ;;
    --only)    ONLY="${2:?--only needs a comma list}"; shift ;;
    --verify)  DO_VERIFY=1 ;;
    --status)  STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---- selection ---------------------------------------------------------------
if [ -n "$ONLY" ]; then
  SELECTED=()
  IFS=',' read -ra wanted <<< "$ONLY"
  for w in "${wanted[@]}"; do
    w="$(echo "$w" | xargs)"
    hit=""
    for n in "${ALL[@]}"; do [ "$w" = "$n" ] && hit="$n"; done
    [ -z "$hit" ] && { echo "unknown repo in --only: $w" >&2; echo "valid: ${ALL[*]}" >&2; exit 2; }
    SELECTED+=("$hit")
  done
else
  SELECTED=("${ALL[@]}")
fi

c_hdr() { printf '\033[1;36m%s\033[0m\n' "$*"; }
c_ok()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
c_warn(){ printf '\033[1;33m%s\033[0m\n' "$*"; }
c_err() { printf '\033[1;31m%s\033[0m\n' "$*"; }

pin_of() { python3 - "$1" <<'PY'
import json,sys
m=json.load(open("MANIFEST.json"))
for f in m["frameworks"]:
    if f["id"]==sys.argv[1]:
        print(f["pin"]["sha"], f["pin"]["tag"], f["packages"][0].get("name",""), f["packages"][0].get("version",""))
        break
PY
}

# ---- status mode -------------------------------------------------------------
if [ "$STATUS_ONLY" = "1" ]; then
  c_hdr "Submodule status (pinned vs checked out)"
  printf '%-30s %-13s %-13s %s\n' REPO PINNED CHECKED OUT STATE
  for n in "${SELECTED[@]}"; do
    read -r sha tag _ _ <<< "$(pin_of "$n")"
    if [ -d "vendor/$n/.git" ] || [ -f "vendor/$n/.git" ]; then
      cur="$(git -C "vendor/$n" rev-parse --short=12 HEAD 2>/dev/null || echo missing)"
      [ "${cur:0:12}" = "${sha:0:12}" ] && st="ok" || st="DRIFT (pin $tag)"
    else
      cur="-"; st="not initialised"
    fi
    printf '%-30s %-13s %-13s %s\n' "$n" "${sha:0:12}" "$cur" "$st"
  done
  exit 0
fi

# ---- banner ------------------------------------------------------------------
TOTAL_HUMAN="~526 MB shallow / ~2.3 GB full history"
[ "$SHALLOW" = "0" ] && c_warn "NOTE: full-history clones are slow and large ($TOTAL_HUMAN)."
[ "$SHALLOW" = "1" ] && echo "Using shallow clones (--depth 1): ~526 MB total."
echo "Repos: ${SELECTED[*]}   jobs=$JOBS"
echo

mkdir -p vendor

# ---- clone/update ------------------------------------------------------------
# A gitlink must exist in the index for `git submodule update` to work.
ensure_gitlink() {
  local n="$1" sha="$2"
  if ! git ls-files --stage "vendor/$n" | grep -q '^160000'; then
    git update-index --add --cacheinfo "160000,$sha,vendor/$n" 2>/dev/null \
      || c_warn "could not stage gitlink for $n (continuing)"
  fi
}

# Do the heavy lifting with a direct clone (safe to run in parallel: it does not
# touch the parent index). The parent index is updated afterwards, sequentially,
# via `git submodule absorbgitdirs`.
clone_one() {
  local n="$1"
  read -r sha tag _ _ <<< "$(pin_of "$n")"
  local dir="vendor/$n"
  ensure_gitlink "$n" "$sha"

  if [ -e "$dir/.git" ]; then
    echo "[$n] already present - fetching tag $tag"
    git -C "$dir" fetch --tags --force --quiet origin 2>/dev/null || c_warn "[$n] fetch failed; keeping current checkout"
    git -C "$dir" checkout --quiet "$sha" 2>/dev/null || c_warn "[$n] could not checkout $sha"
    return 0
  fi

  rm -rf "$dir"
  echo "[$n] cloning $tag ($sha) ..."
  local depth=()
  [ "$SHALLOW" = "1" ] && depth=(--depth 1)
  if git clone --quiet --no-checkout "${depth[@]}" --branch "$tag" \
        "https://github.com/$n.git" "$dir" 2>/dev/null \
     || git clone --quiet "${depth[@]}" "https://github.com/$n.git" "$dir" 2>/dev/null; then
    git -C "$dir" checkout --quiet "$sha" 2>/dev/null || git -C "$dir" checkout --quiet "$tag" 2>/dev/null
    c_ok "[$n] ok ($(git -C "$dir" rev-parse --short=12 HEAD))"
  else
    c_err "[$n] clone FAILED"
    return 1
  fi
}

FAILED=()
if [ "$JOBS" -gt 1 ]; then
  pids=()
  for n in "${SELECTED[@]}"; do
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 0.3; done
    ( clone_one "$n" ) & pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || FAILED+=("pid:$p"); done
else
  for n in "${SELECTED[@]}"; do clone_one "$n" || FAILED+=("$n"); done
fi

# ---- adopt the clones as proper submodules (parents the gitdir into .git/modules)
echo
for n in "${SELECTED[@]}"; do
  [ -e "vendor/$n/.git" ] && { git submodule absorbgitdirs "vendor/$n" >/dev/null 2>&1 || true; }
done
git submodule init >/dev/null 2>&1 || true

# ---- summary -----------------------------------------------------------------
echo
c_hdr "Result"
printf '%-30s %-13s %-10s %s\n' REPO HEAD PIN MATCH DIR-SIZE
for n in "${SELECTED[@]}"; do
  read -r sha tag _ _ <<< "$(pin_of "$n")"
  if [ -e "vendor/$n/.git" ]; then
    cur="$(git -C "vendor/$n" rev-parse --short=12 HEAD)"
    [ "${cur:0:12}" = "${sha:0:12}" ] && ok="yes" || ok="NO"
    printf '%-30s %-13s %-10s %s\n' "$n" "$cur" "$ok" "$(du -sh "vendor/$n" 2>/dev/null | cut -f1)"
  else
    printf '%-30s %-13s %-10s %s\n' "$n" "-" "-" "MISSING"
  fi
done

echo
echo "Disk used by vendor/: $(du -sh vendor 2>/dev/null | cut -f1)"
echo "Next: ./scripts/verify.sh   (installs pinned packages + runs smoke tests)"

if [ "${#FAILED[@]}" -gt 0 ]; then
  c_err "${#FAILED[@]} repo(s) failed: ${FAILED[*]}"
  exit 1
fi

if [ "$DO_VERIFY" = "1" ]; then
  echo; exec "$ROOT/scripts/verify.sh"
fi
