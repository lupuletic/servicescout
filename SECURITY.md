# Security Policy

ServiceScout is an alpha, local-first operator tool for indexing source
repositories into a catalog served over MCP. Treat it like any other tool that
can read private source code and use developer credentials.

## Supported Security Model

- Run it on a trusted workstation, VM, or private network.
- Keep the default localhost binds unless you put it behind your own VPN,
  firewall, SSH tunnel, SSO proxy, or authenticated reverse proxy.
- Do not expose the dashboard or `/mcp` directly to the public internet.
- Use a dedicated workspace and data directory for private indexing runs.
- Use read-only or least-privilege credentials where possible.

## Credential Access

The Compose stack can mount these host credentials read-only:

- GitHub CLI auth from `~/.config/gh`
- Google Cloud ADC from `~/.config/gcloud`
- Codex CLI auth from `~/.codex`
- Claude Code auth from `~/.claude`

Read-only mounts prevent accidental writes to those credential directories, but
the running process can still use the credentials. Only run ServiceScout from
images and checkouts you trust.

## Data Egress

Extraction is intentionally LLM-assisted. Depending on configuration,
source-derived prompts, file snippets, catalog summaries, and extracted
metadata may be sent to:

- the selected extractor harness (`codex` or `claude`)
- the selected embedding provider when embeddings are enabled

Leave `GOOGLE_CLOUD_PROJECT` empty to skip Vertex AI embeddings and use
lexical-only search. Do not index repositories whose contents cannot be sent
to your configured LLM or embedding provider.

The current extraction harnesses run provider CLIs inside the container. Local
developer runs can mount `~/.codex` and `~/.claude`; remote VM deployments
should prefer a dedicated API-key or credential-proxy harness once that mode is
implemented, rather than copying personal interactive login state onto a server.

## Network Exposure

The MCP server exposes read-only catalog tools. The dashboard exposes read
APIs and operator controls that can start crawler and scheduler work using the
mounted credentials. There is no built-in application-level auth in this alpha.

For shared deployments, put both the dashboard and `/mcp` behind an existing
identity layer and network boundary. The bundled nginx profile is only a small
reverse proxy; it is not an identity provider.

## Secrets and Local State

The repository ignores local runtime state such as `.env`, `data/`,
`certs/*.pem`, virtual environments, dependency folders, and generated eval
workspaces. Before publishing a fork or release archive, run a secrets scan and
confirm those paths are not included.

Suggested local checks:

```bash
git status --short
trufflehog git file://$(pwd) --no-update --only-verified --fail
git ls-files | grep -E '(^|/)(\\.env|data/|certs/.*\\.pem|catalog\\.json|catalog\\.kuzu)$'
```

The last command should print nothing except committed example files.

## Reporting

Report vulnerabilities through the repository's GitHub security advisory flow
or by opening a private issue with the maintainers. Do not include private
catalogs, credentials, or proprietary source snippets in public issues.
