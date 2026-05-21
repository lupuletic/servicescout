#!/usr/bin/env bash
# ServiceScout container entrypoint.
#
# On Linux hosts the bind-mounted /workspace and /data are owned by the host
# user, so we re-establish the `cg` user at that uid/gid before exec-ing the
# real command. On macOS Docker handles this transparently.

set -e

# Codex keeps mutable session / app-server state in its home dir. Compose mounts
# ~/.codex read-only for safety, which makes `codex exec` fail with
# "failed to initialize in-process app-server client: Read-only file system".
# If the mounted home is read-only, copy it to a writable location and point
# codex at it via CODEX_HOME — preserving auth while restoring writability.
if [[ -d "$HOME/.codex" && ! -w "$HOME/.codex" ]]; then
  if cp -a "$HOME/.codex" "$HOME/.codex-rw" 2>/dev/null; then
    chmod -R u+w "$HOME/.codex-rw" 2>/dev/null || true
    export CODEX_HOME="$HOME/.codex-rw"
  fi
fi

if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
  if [[ "$(id -u cg)" != "${HOST_UID}" ]] || [[ "$(id -g cg)" != "${HOST_GID}" ]]; then
    # Re-create cg at the host uid/gid (sudo-less; image installs run as root
    # before USER cg directive, so this branch only fires when entrypoint is
    # invoked from root e.g. via `docker run -u 0:0 ...`).
    if [[ "$(id -u)" == "0" ]]; then
      groupmod -g "${HOST_GID}" cg
      usermod -u "${HOST_UID}" -g "${HOST_GID}" cg
      chown -R cg:cg /home/cg /data /workspace 2>/dev/null || true
      exec gosu cg "$@"
    fi
  fi
fi

exec "$@"
