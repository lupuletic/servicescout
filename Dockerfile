# syntax=docker/dockerfile:1.7
#
# ServiceScout — single image used by mcp, dashboard, and crawler services.
# - Python 3.12 + project deps via pip
# - Node 22 + codex / claude / gh CLIs (the crawler needs these; bundling in
#   the same image keeps the service count to one).
# - Non-root user `cg` (uid/gid overridable via build arg + entrypoint).

FROM nikolaik/python-nodejs:python3.12-nodejs22-slim AS base

ARG GH_VERSION=2.62.0
ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PIP_NO_INDEX=
ARG PIP_FIND_LINKS=/tmp/wheels
ARG NPM_CONFIG_REGISTRY=

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git ripgrep tini gosu \
    && rm -rf /var/lib/apt/lists/*

# Optional: trust extra CA certificates from ./certs/ (e.g. corporate TLS-
# inspecting proxies). Real PEM files stay git-ignored; certs/.gitkeep keeps
# the directory present for clean Docker builds.
COPY certs/ /tmp/extra-ca/
RUN if [ -d /tmp/extra-ca ] && ls /tmp/extra-ca/*.pem >/dev/null 2>&1; then \
        for f in /tmp/extra-ca/*.pem; do \
            base="/usr/local/share/ca-certificates/$(basename "$f" .pem)"; \
            awk -v base="$base" '/-----BEGIN CERTIFICATE-----/{n++} n{print > base "-" n ".crt"}' "$f"; \
        done && \
        update-ca-certificates && \
        echo "Trusted extra CA bundles: $(ls /tmp/extra-ca/*.pem | wc -l)"; \
    fi && rm -rf /tmp/extra-ca

ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# gh CLI via apt (signed, works in restricted networks)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# codex + claude CLIs — best-effort install.
# Only the crawler service actually uses these; mcp + dashboard work without
# them. In restricted networks, install on host and mount ~/.codex / ~/.claude
# as documented in README.
RUN if [ -n "$NPM_CONFIG_REGISTRY" ]; then npm config set registry "$NPM_CONFIG_REGISTRY"; fi \
    && npm install -g @openai/codex@latest @anthropic-ai/claude-code@latest \
    && npm cache clean --force \
    || echo "WARN: codex / claude CLI install failed; crawler service must run with host-mounted CLIs."

# Python venv with project deps
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
COPY vendor/wheels/ /tmp/wheels/
RUN pip_args="" \
    && if [ -n "$PIP_NO_INDEX" ]; then pip_args="$pip_args --no-index"; fi \
    && if [ -n "$PIP_FIND_LINKS" ] && [ -d "$PIP_FIND_LINKS" ] && find "$PIP_FIND_LINKS" -type f | grep -q .; then pip_args="$pip_args --find-links $PIP_FIND_LINKS"; fi \
    && if [ -n "$PIP_INDEX_URL" ]; then pip_args="$pip_args --index-url $PIP_INDEX_URL"; fi \
    && if [ -n "$PIP_EXTRA_INDEX_URL" ]; then pip_args="$pip_args --extra-index-url $PIP_EXTRA_INDEX_URL"; fi \
    && if [ -n "$PIP_TRUSTED_HOST" ]; then pip_args="$pip_args --trusted-host $PIP_TRUSTED_HOST"; fi \
    && /opt/venv/bin/pip install $pip_args -r /tmp/requirements.txt

WORKDIR /app
COPY . /app/

# Build the React dashboard. The same Python-Nodejs base image has npm,
# so we do this in-place rather than a separate stage. node_modules is
# discarded afterwards to keep the image slim.
RUN if [ -d frontend ]; then \
        cd frontend \
        && npm install --no-audit --no-fund \
        && npm run build \
        && rm -rf node_modules \
        || (echo "WARN: frontend build failed — dashboard will show fallback page" && rm -rf node_modules) ; \
    fi

# Non-root user with home for credential mounts. The base image already has a
# user `pn` at UID 1000, so create `cg` at 1001. The entrypoint re-aligns to
# HOST_UID/HOST_GID at runtime via gosu.
RUN groupadd -g 1001 cg \
    && useradd -u 1001 -g 1001 -m -s /bin/bash cg \
    && mkdir -p /data /workspace && chown -R cg:cg /app /data /workspace /home/cg

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER cg
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "mcp_server.py", "--catalog", "/data/catalog.json", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
