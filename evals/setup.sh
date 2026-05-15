#!/usr/bin/env bash
# Clone the eval workspace at pinned SHAs (or pin them on first run).
#
# Usage:
#   ./evals/setup.sh                    # clone + pin if not already pinned
#   ./evals/setup.sh --refresh          # fetch latest + re-pin (drops lock)
#   ./evals/setup.sh --catalog          # also build the catalog after cloning
#
# Layout produced:
#   evals/workspace/<repo>/             # clones
#   evals/workspace.lock.json           # pinned SHAs (committed)
#
# Catalog build is a separate step because it costs LLM tokens. Run with
# --catalog when you want a fresh extraction, or use the project's normal
# crawler flow targeting CATALOG_OUT=evals/data/catalog.json.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_JSON="$HERE/workspace.json"
LOCK_JSON="$HERE/workspace.lock.json"
CLONE_ROOT="$HERE/workspace"
CATALOG_DIR="$HERE/data"

REFRESH=0
BUILD_CATALOG=0
for arg in "$@"; do
  case "$arg" in
    --refresh) REFRESH=1 ;;
    --catalog) BUILD_CATALOG=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$CLONE_ROOT"

if [[ $REFRESH -eq 1 && -f "$LOCK_JSON" ]]; then
  rm "$LOCK_JSON"
fi

# Parse repo list with python (avoids jq dependency)
mapfile -t REPOS < <(python3 -c "
import json, sys
with open('$WORKSPACE_JSON') as f:
    ws = json.load(f)
for r in ws['repos']:
    print(f\"{r['id']}\t{r['url']}\")
")

declare -A LOCKED
if [[ -f "$LOCK_JSON" ]]; then
  while IFS=$'\t' read -r id sha; do
    LOCKED[$id]=$sha
  done < <(python3 -c "
import json
with open('$LOCK_JSON') as f:
    lk = json.load(f)
for id_, sha in lk['repos'].items():
    print(f'{id_}\t{sha}')
")
fi

declare -A NEW_LOCKS
for line in "${REPOS[@]}"; do
  IFS=$'\t' read -r id url <<< "$line"
  name="${id##*/}"
  target="$CLONE_ROOT/$name"

  if [[ ! -d "$target/.git" ]]; then
    echo "cloning $id"
    git clone --quiet "$url" "$target"
  fi

  if [[ -n "${LOCKED[$id]:-}" ]]; then
    pinned_sha="${LOCKED[$id]}"
    current_sha="$(git -C "$target" rev-parse HEAD)"
    if [[ "$current_sha" != "$pinned_sha" ]]; then
      echo "  pinning $id to $pinned_sha"
      git -C "$target" fetch --quiet origin "$pinned_sha" || true
      git -C "$target" checkout --quiet "$pinned_sha"
    fi
    NEW_LOCKS[$id]="$pinned_sha"
  else
    git -C "$target" fetch --quiet origin
    head_sha="$(git -C "$target" rev-parse HEAD)"
    echo "  pinning new: $id -> $head_sha"
    NEW_LOCKS[$id]="$head_sha"
  fi
done

python3 - <<PY
import json
locks = {}
$(for id in "${!NEW_LOCKS[@]}"; do echo "locks['$id'] = '${NEW_LOCKS[$id]}'"; done)
out = {"version": 1, "repos": locks}
with open("$LOCK_JSON", "w") as f:
    json.dump(out, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"wrote {len(locks)} entries to $LOCK_JSON")
PY

if [[ $BUILD_CATALOG -eq 1 ]]; then
  echo ""
  echo "Building catalog into $CATALOG_DIR/catalog.json ..."
  mkdir -p "$CATALOG_DIR"
  if [[ -z "${ANTHROPIC_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
    echo "  WARN: no ANTHROPIC_API_KEY / OPENAI_API_KEY exported" >&2
    echo "        skipping catalog build; re-run setup.sh --catalog after exporting one" >&2
  else
    echo "  (catalog build wiring: see evals/README.md — keeps eval and prod catalogs isolated)"
  fi
fi

echo ""
echo "done. next steps:"
echo "  1) build a catalog into evals/data/catalog.json (see evals/README.md)"
echo "  2) python evals/runner.py            # catalog-only metrics (free, fast)"
echo "  3) python evals/runner.py --agent    # add agent-based metrics (\$\$)"
