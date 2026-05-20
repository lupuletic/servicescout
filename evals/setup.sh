#!/usr/bin/env bash
# Clone the eval workspace at pinned SHAs (or pin them on first run).
#
# Usage:
#   ./evals/setup.sh                                  # sock-shop clone + pin
#   ./evals/setup.sh --workspace online-boutique      # clone + pin another eval workspace
#   ./evals/setup.sh --refresh                        # fetch latest + re-pin
#   ./evals/setup.sh --catalog                        # print catalog build hint
#
# Layout produced:
#   sock-shop: evals/workspace/<repo>/ and evals/workspace.lock.json
#   others:    evals/workspaces/<name>/workspace/<repo>/ and workspace.lock.json
#
# Catalog build is a separate step because it costs LLM tokens. Run with
# --catalog when you want a fresh extraction, or use the project's normal
# crawler flow targeting CATALOG_OUT=evals/data/catalog.json.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
WORKSPACE_NAME="sock-shop"
REFRESH=0
BUILD_CATALOG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE_NAME="$2"; shift 2 ;;
    --refresh) REFRESH=1; shift ;;
    --catalog) BUILD_CATALOG=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

eval "$("$PYTHON_BIN" "$HERE/workspace_paths.py" --workspace "$WORKSPACE_NAME" --shell)"

WORKSPACE_JSON="$SS_WORKSPACE_JSON"
LOCK_JSON="$SS_LOCK_JSON"
CLONE_ROOT="$SS_CLONE_ROOT"
CATALOG_DIR="$SS_DATA_DIR"

if [[ ! -f "$WORKSPACE_JSON" ]]; then
  echo "workspace spec not found: $WORKSPACE_JSON" >&2
  exit 2
fi

mkdir -p "$CLONE_ROOT"

if [[ $REFRESH -eq 1 && -f "$LOCK_JSON" ]]; then
  rm "$LOCK_JSON"
fi

# Parse repo list with python (avoids jq dependency)
mapfile -t REPOS < <("$PYTHON_BIN" -c "
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
  done < <("$PYTHON_BIN" -c "
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

"$PYTHON_BIN" - <<PY
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
echo "  1) ./evals/build_eval_catalog.sh --workspace $SS_NAME"
echo "  2) python build_kuzu.py --catalog $SS_DATA_DIR/catalog.json --db $SS_DATA_DIR/catalog.kuzu"
echo "  3) python evals/runner.py --workspace $SS_NAME"
