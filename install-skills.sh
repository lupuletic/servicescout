#!/usr/bin/env bash
#
# ServiceScout — install Claude Code skills system-wide.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lupuletic/servicescout/main/install-skills.sh | bash
#   # or, from a cloned checkout:
#   ./install-skills.sh
#
# What it does: copies the `servicescout` and `journey` skills into
# ~/.claude/skills/ so Claude Code picks them up automatically.

set -euo pipefail

REPO_RAW="${SERVICESCOUT_RAW:-https://raw.githubusercontent.com/lupuletic/servicescout/main}"
TARGET="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
SKILLS=("servicescout" "journey")

echo "▸ Installing ServiceScout skills into $TARGET"
mkdir -p "$TARGET"

if [ -f "$(dirname "$0")/.claude/skills/servicescout/SKILL.md" ]; then
  # Running from a local checkout.
  SRC="$(cd "$(dirname "$0")/.claude/skills" && pwd)"
  for skill in "${SKILLS[@]}"; do
    rm -rf "$TARGET/$skill"
    cp -r "$SRC/$skill" "$TARGET/$skill"
    echo "  ✓ $skill (from local checkout)"
  done
else
  # Running via curl | bash — fetch from GitHub.
  for skill in "${SKILLS[@]}"; do
    rm -rf "$TARGET/$skill"
    mkdir -p "$TARGET/$skill"
    curl -fsSL "$REPO_RAW/.claude/skills/$skill/SKILL.md" -o "$TARGET/$skill/SKILL.md"
    echo "  ✓ $skill (from $REPO_RAW)"
  done
fi

echo ""
echo "Done. Restart Claude Code (or your MCP-aware agent) so it picks up the new skills."
echo ""
echo "Next: start the ServiceScout MCP server on http://localhost:8765/mcp"
echo "      (see https://github.com/lupuletic/servicescout#quickstart)"
