#!/usr/bin/env bash
#
# ServiceScout — one-line installer.
#
# Installs the ServiceScout skills into ~/.claude/skills/ and registers the
# ServiceScout MCP server with your coding agent (Claude Code, Codex, or both).
#
# Interactive (asks which agent + MCP URL):
#   curl -fsSL https://raw.githubusercontent.com/servicescout/servicescout/main/install.sh | bash
#
# Headless (pass flags via `bash -s --`):
#   curl -fsSL https://raw.githubusercontent.com/servicescout/servicescout/main/install.sh \
#     | bash -s -- --agent claude --url http://127.0.0.1:8765/mcp --yes
#
# Flags:
#   --agent claude|codex|both|skip   Which agent(s) to register the MCP with
#   --url   URL                       MCP server URL (default http://127.0.0.1:8765/mcp)
#   --skills-only                     Install skills only, don't register MCP
#   --yes / -y                        Accept defaults, no prompts
#   --help / -h                       Show this message

set -euo pipefail

REPO_RAW="${SERVICESCOUT_RAW:-https://raw.githubusercontent.com/servicescout/servicescout/main}"
TARGET="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
SKILLS=("servicescout" "journey")

AGENT=""
MCP_URL=""
ASSUME_YES=0
SKILLS_ONLY=0

usage() {
  sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="${2:-}"; shift 2 ;;
    --url)   MCP_URL="${2:-}"; shift 2 ;;
    --skills-only) SKILLS_ONLY=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown flag: $1" >&2; usage 1 ;;
  esac
done

# Open /dev/tty for prompts even when stdin is a pipe (curl | bash).
if [ -e /dev/tty ] && [ -r /dev/tty ]; then
  TTY=/dev/tty
else
  TTY=""
  ASSUME_YES=1   # no tty → can't prompt
fi

prompt() {
  # prompt VAR "Question" "default"
  local __var=$1 __q=$2 __def=${3:-} __ans
  if [ "$ASSUME_YES" = "1" ] || [ -z "$TTY" ]; then
    eval "$__var=\"\$__def\""
    return
  fi
  if [ -n "$__def" ]; then
    printf "  %s [%s]: " "$__q" "$__def" > "$TTY"
  else
    printf "  %s: " "$__q" > "$TTY"
  fi
  IFS= read -r __ans < "$TTY" || __ans=""
  [ -z "$__ans" ] && __ans=$__def
  eval "$__var=\"\$__ans\""
}

echo "▸ ServiceScout installer"
echo ""

# ----- Step 1: install skills -----
echo "[1/2] Installing skills into $TARGET"
mkdir -p "$TARGET"
if [ -f "$(dirname "$0")/.claude/skills/servicescout/SKILL.md" ]; then
  SRC="$(cd "$(dirname "$0")/.claude/skills" && pwd)"
  for skill in "${SKILLS[@]}"; do
    rm -rf "$TARGET/$skill"
    cp -r "$SRC/$skill" "$TARGET/$skill"
    echo "      ✓ $skill (from local checkout)"
  done
else
  for skill in "${SKILLS[@]}"; do
    rm -rf "$TARGET/$skill"
    mkdir -p "$TARGET/$skill"
    curl -fsSL "$REPO_RAW/.claude/skills/$skill/SKILL.md" -o "$TARGET/$skill/SKILL.md"
    echo "      ✓ $skill (from $REPO_RAW)"
  done
fi

if [ "$SKILLS_ONLY" = "1" ]; then
  echo ""
  echo "Skills installed. Skipping MCP registration (--skills-only)."
  exit 0
fi

# ----- Step 2: register MCP server -----
echo ""
echo "[2/2] Register the ServiceScout MCP server with your coding agent"

if [ -z "$AGENT" ]; then
  echo ""
  echo "  Available agents:"
  echo "    1) Claude Code   (claude CLI)"
  echo "    2) Codex         (codex CLI)"
  echo "    3) Both"
  echo "    4) Skip — I'll wire it up myself"
  prompt AGENT "Choose [1-4]" "1"
  case "$AGENT" in
    1|claude) AGENT="claude" ;;
    2|codex)  AGENT="codex" ;;
    3|both)   AGENT="both" ;;
    4|skip)   AGENT="skip" ;;
    *) echo "unknown choice: $AGENT" >&2; exit 1 ;;
  esac
fi

if [ "$AGENT" = "skip" ]; then
  echo ""
  echo "Done. Skills installed; MCP not registered. To register later:"
  echo "  claude mcp add --scope user --transport http servicescout http://127.0.0.1:8765/mcp"
  echo "  codex  mcp add servicescout --url http://127.0.0.1:8765/mcp"
  exit 0
fi

[ -z "$MCP_URL" ] && prompt MCP_URL "MCP server URL" "http://127.0.0.1:8765/mcp"

register_claude() {
  if ! command -v claude >/dev/null 2>&1; then
    echo "      ⚠ 'claude' CLI not found on PATH. Install Claude Code first:"
    echo "        https://docs.claude.com/claude-code"
    return 1
  fi
  # Idempotent: remove any prior entry, then add fresh.
  claude mcp remove servicescout --scope user >/dev/null 2>&1 || true
  claude mcp add servicescout --scope user --transport http "$MCP_URL"
  echo "      ✓ Registered with Claude Code  ($MCP_URL)"
}

register_codex() {
  if command -v codex >/dev/null 2>&1; then
    codex mcp remove servicescout >/dev/null 2>&1 || true
    codex mcp add servicescout --url "$MCP_URL"
    echo "      ✓ Registered with Codex  ($MCP_URL)"
    return 0
  fi
  # Fallback: write directly to ~/.codex/config.toml if the CLI isn't installed.
  local cfg="$HOME/.codex/config.toml"
  mkdir -p "$HOME/.codex"
  touch "$cfg"
  if grep -q '^\[mcp_servers\.servicescout\]' "$cfg" 2>/dev/null; then
    echo "      • servicescout entry already in $cfg — leaving as is"
  else
    {
      echo ""
      echo "[mcp_servers.servicescout]"
      echo "url = \"$MCP_URL\""
    } >> "$cfg"
    echo "      ✓ Appended servicescout entry to $cfg"
    echo "        (install the codex CLI later to manage entries via 'codex mcp')"
  fi
}

echo ""
case "$AGENT" in
  claude) register_claude ;;
  codex)  register_codex ;;
  both)   register_claude || true; register_codex ;;
esac

echo ""
echo "Done."
echo ""
echo "  Make sure the MCP server is running:"
echo "    docker compose up -d mcp dashboard          # via docker"
echo "    python -m servicescout.mcp_server --transport streamable-http   # locally"
echo ""
echo "  Then restart your coding agent and ask:"
echo "    \"trace the login flow end-to-end\""
echo "    \"what consumes the orders API?\""
