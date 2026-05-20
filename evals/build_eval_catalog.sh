#!/usr/bin/env bash
# Build an eval-specific catalog without touching the project-level data/.
#
# This script:
#   1. Runs ServiceScout's extractor.py against repos cloned by setup.sh.
#   2. Writes per-repo extraction JSONs into that workspace's data/extractions/.
#   3. Calls build_catalog.py to merge them into that workspace's catalog.json.
#
# Cost: LLM tokens (uses `codex` CLI). Typical first-run with
# --effort medium and --model gpt-5.4-mini is single-digit dollars.
#
# Usage:
#   ./evals/build_eval_catalog.sh                              # sock-shop
#   ./evals/build_eval_catalog.sh --workspace online-boutique  # second benchmark
#   ./evals/build_eval_catalog.sh --model gpt-5.4 --effort high
#   ./evals/build_eval_catalog.sh --only front-end,catalogue   # subset for smoke test
#   ./evals/build_eval_catalog.sh --skip-extraction            # only run merge step

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"

WORKSPACE_NAME="sock-shop"
PROVIDER="codex"
MODEL="gpt-5.4-mini"
EFFORT="medium"
TIMEOUT="900"
ONLY=""
SKIP_EXTRACTION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace) WORKSPACE_NAME="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --model)    MODEL="$2";    shift 2 ;;
    --effort)   EFFORT="$2";   shift 2 ;;
    --timeout-seconds) TIMEOUT="$2"; shift 2 ;;
    --only)     ONLY="$2";     shift 2 ;;
    --skip-extraction) SKIP_EXTRACTION=1; shift ;;
    -h|--help) sed -n '1,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

eval "$("$PYTHON_BIN" "$HERE/workspace_paths.py" --workspace "$WORKSPACE_NAME" --shell)"

WS_ROOT="$SS_CLONE_ROOT"
OUT_DIR="$SS_DATA_DIR"
EXTRACT_DIR="$SS_EXTRACTION_DIR"
ORGS_CONFIG="$SS_ORGS_CONFIG"
WORKSPACE_JSON="$SS_WORKSPACE_JSON"

if [[ ! -f "$WORKSPACE_JSON" ]]; then
  echo "workspace spec not found: $WORKSPACE_JSON" >&2
  exit 2
fi
if [[ ! -d "$WS_ROOT" ]]; then
  echo "workspace root not found: $WS_ROOT" >&2
  echo "run: ./evals/setup.sh --workspace $SS_NAME" >&2
  exit 2
fi

mapfile -t ALL_REPO_LINES < <("$PYTHON_BIN" -c "
import json
with open('$ORGS_CONFIG') as f:
    cfg = json.load(f)
units = cfg.get('repo_units') or cfg.get('extract_units') or []
if not units:
    with open('$WORKSPACE_JSON') as f:
        ws = json.load(f)
    units = ws['repos']
for r in units:
    repo_id = r['id']
    local = r.get('name') or r.get('local_name') or repo_id.rstrip('/').split('/')[-1]
    print(f'{repo_id}\t{local}')
")

if [[ -n "$ONLY" ]]; then
  IFS=',' read -ra ONLY_ITEMS <<< "$ONLY"
  REPO_LINES=()
  for line in "${ALL_REPO_LINES[@]}"; do
    IFS=$'\t' read -r repo_id local <<< "$line"
    for item in "${ONLY_ITEMS[@]}"; do
      if [[ "$item" == "$repo_id" || "$item" == "$local" ]]; then
        REPO_LINES+=("$line")
      fi
    done
  done
else
  REPO_LINES=("${ALL_REPO_LINES[@]}")
fi

if [[ ${#REPO_LINES[@]} -eq 0 ]]; then
  echo "no repositories selected for workspace $SS_NAME" >&2
  exit 2
fi

mkdir -p "$EXTRACT_DIR"

cd "$PROJECT_ROOT"

if [[ $SKIP_EXTRACTION -eq 0 ]]; then
  for line in "${REPO_LINES[@]}"; do
    IFS=$'\t' read -r repo_id local <<< "$line"
    echo ""
    echo "==> extracting $repo_id"
    "$PYTHON_BIN" extractor.py "$repo_id" \
      --root "$WS_ROOT" \
      --workspace "$ORGS_CONFIG" \
      --output-dir "$EXTRACT_DIR" \
      --provider "$PROVIDER" \
      --model "$MODEL" \
      --effort "$EFFORT" \
      --timeout-seconds "$TIMEOUT" \
      --stream-logs || {
        echo "  WARN: extraction failed for $repo_id (continuing)" >&2
      }
  done
fi

echo ""
echo "==> merging into $OUT_DIR/catalog.json"
"$PYTHON_BIN" build_catalog.py \
  --root "$WS_ROOT" \
  --workspace "$ORGS_CONFIG" \
  --catalog-dir "$EXTRACT_DIR" \
  --output "$OUT_DIR/catalog.json"

echo ""
echo "done."
echo "  catalog: $OUT_DIR/catalog.json"
echo "  next:    python build_kuzu.py --catalog $OUT_DIR/catalog.json --db $OUT_DIR/catalog.kuzu"
echo "           python evals/runner.py --workspace $SS_NAME"
