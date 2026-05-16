#!/usr/bin/env bash
# Build the eval-specific catalog at evals/data/catalog.json without
# touching the project-level data/ directory.
#
# This script:
#   1. Runs ServiceScout's extractor.py against each of the 9 sock-shop
#      repos already cloned by setup.sh.
#   2. Writes per-repo extraction JSONs into evals/data/extractions/.
#   3. Calls build_catalog.py to merge them into evals/data/catalog.json.
#
# Cost: LLM tokens (uses `codex` CLI). Typical first-run with
# --effort medium and --model gpt-5.4-mini is single-digit dollars.
#
# Usage:
#   ./evals/build_eval_catalog.sh                # default: codex / gpt-5.4-mini / medium
#   ./evals/build_eval_catalog.sh --model gpt-5.4 --effort high
#   ./evals/build_eval_catalog.sh --only front-end,catalogue   # subset for smoke test
#   ./evals/build_eval_catalog.sh --skip-extraction            # only run merge step

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$HERE/.." && pwd)"

WS_ROOT="$HERE/workspace"
OUT_DIR="$HERE/data"
EXTRACT_DIR="$OUT_DIR/extractions"
ORGS_CONFIG="$HERE/workspace_orgs.json"

PROVIDER="codex"
MODEL="gpt-5.4-mini"
EFFORT="medium"
TIMEOUT="900"
ONLY=""
SKIP_EXTRACTION=0

while [[ $# -gt 0 ]]; do
  case "$1" in
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

ALL_REPOS=(front-end catalogue carts orders payment shipping queue-master user load-test)

if [[ -n "$ONLY" ]]; then
  IFS=',' read -ra REPOS <<< "$ONLY"
else
  REPOS=("${ALL_REPOS[@]}")
fi

mkdir -p "$EXTRACT_DIR"

cd "$PROJECT_ROOT"

if [[ $SKIP_EXTRACTION -eq 0 ]]; then
  for repo in "${REPOS[@]}"; do
    echo ""
    echo "==> extracting microservices-demo/$repo"
    python3 extractor.py "microservices-demo/$repo" \
      --root "$WS_ROOT" \
      --workspace "$ORGS_CONFIG" \
      --output-dir "$EXTRACT_DIR" \
      --provider "$PROVIDER" \
      --model "$MODEL" \
      --effort "$EFFORT" \
      --timeout-seconds "$TIMEOUT" || {
        echo "  WARN: extraction failed for $repo (continuing)" >&2
      }
  done
fi

echo ""
echo "==> merging into $OUT_DIR/catalog.json"
python3 build_catalog.py \
  --root "$WS_ROOT" \
  --workspace "$ORGS_CONFIG" \
  --catalog-dir "$EXTRACT_DIR" \
  --output "$OUT_DIR/catalog.json"

echo ""
echo "done."
echo "  catalog: $OUT_DIR/catalog.json"
echo "  next:    python evals/runner.py"
