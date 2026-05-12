"""Interactive first-run wizard for ServiceScout.

  python init_wizard.py            # interactive
  python init_wizard.py --check    # validate existing config, no prompts

Walks the user through workspace.json, LLM provider auth, Google Cloud
project (for embeddings), and budget. Writes:

  workspace.json                       — orgs + exclusions for the crawler
  .env                                 — secrets + runtime config for docker compose

Designed to be re-runnable. Detects existing config and offers keep / edit / reset.
The wizard is the only place we ask for secrets; nothing else in the project
prompts the user.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).parent
WORKSPACE_PATH = HERE / "workspace.json"
ENV_PATH = HERE / ".env"
ENV_EXAMPLE_PATH = HERE / ".env.example"


def _intro() -> None:
    print()
    print("─" * 60)
    print(" ServiceScout — first-run setup")
    print("─" * 60)
    print()
    print(" This wizard will configure:")
    print("   • GitHub orgs to scan")
    print("   • LLM provider for extraction (codex / claude)")
    print("   • Google Cloud project for embeddings (optional)")
    print("   • Budget cap and reasonable defaults")
    print()


def _have_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _gh_authed() -> bool:
    if not _have_tool("gh"):
        return False
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return r.returncode == 0


def _gh_list_orgs() -> list[str]:
    if not _gh_authed():
        return []
    r = subprocess.run(
        ["gh", "api", "user/orgs", "--paginate", "--jq", ".[].login"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def _gcloud_adc_present() -> bool:
    path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    return path.exists()


def _gcloud_project() -> str:
    if not _have_tool("gcloud"):
        return ""
    r = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def _step_orgs() -> tuple[list[str], list[str]]:
    import questionary
    print()
    print("[1/5] GitHub orgs to scan")
    print()
    if not _gh_authed():
        print("  ! gh CLI is not authenticated.")
        print("    Run `gh auth login` in another terminal, then continue.")
        if not questionary.confirm("Continue without listing orgs (you'll paste them manually)?", default=False).ask():
            sys.exit(1)
        raw = questionary.text(
            "GitHub orgs (comma-separated, e.g. acme-payments,acme-platform)"
        ).ask()
        orgs = [o.strip() for o in (raw or "").split(",") if o.strip()]
    else:
        available = _gh_list_orgs()
        if not available:
            print("  ! Could not list your GitHub orgs via gh. You can still type them.")
            raw = questionary.text("GitHub orgs (comma-separated)").ask()
            orgs = [o.strip() for o in (raw or "").split(",") if o.strip()]
        else:
            orgs = questionary.checkbox(
                f"Pick orgs to scan ({len(available)} available)",
                choices=available,
            ).ask() or []
    excluded_raw = questionary.text(
        "Repos to exclude (comma-separated, optional, format org/repo)"
    ).ask()
    excluded = [r.strip() for r in (excluded_raw or "").split(",") if r.strip()]
    return orgs, excluded


def _step_llm() -> dict[str, str]:
    import questionary
    print()
    print("[2/5] LLM provider for repo extraction")
    print()
    provider = questionary.select(
        "Provider",
        choices=[
            {"name": "codex (OpenAI) — typical $1.00–1.50/repo with gpt-5.4-mini high effort", "value": "codex"},
            {"name": "claude (Anthropic) — typical $0.80–1.30/repo with sonnet medium effort", "value": "claude"},
        ],
    ).ask()
    if provider == "codex":
        model = questionary.select(
            "Model",
            choices=["gpt-5.4-mini", "gpt-5.4"],
            default="gpt-5.4-mini",
        ).ask()
        api_key = questionary.password(
            "OpenAI API key (leave empty if `codex login` is already set up)",
        ).ask() or ""
    else:
        model = questionary.select(
            "Model",
            choices=["sonnet", "opus"],
            default="sonnet",
        ).ask()
        api_key = questionary.password(
            "Anthropic API key (leave empty if `claude` is logged in)",
        ).ask() or ""
    effort = questionary.select(
        "Reasoning effort",
        choices=["high", "medium", "low"],
        default="high",
    ).ask()
    return {
        "LLM_PROVIDER": provider,
        "LLM_MODEL": model,
        "LLM_EFFORT": effort,
        "OPENAI_API_KEY": api_key if provider == "codex" else "",
        "ANTHROPIC_API_KEY": api_key if provider == "claude" else "",
    }


def _step_gcp() -> dict[str, str]:
    import questionary
    print()
    print("[3/5] Embeddings (Google Vertex AI)")
    print()
    if not _gcloud_adc_present():
        print("  ! No Google Cloud ADC found.")
        print("    Run `gcloud auth application-default login` for vector search.")
        skip = not questionary.confirm("Set up embeddings now?", default=False).ask()
        if skip:
            print("    Skipping. Catalog will fall back to lexical-only retrieval.")
            return {"GOOGLE_CLOUD_PROJECT": "", "GOOGLE_CLOUD_LOCATION": "us-central1"}
    detected = _gcloud_project() or ""
    project = questionary.text(
        "GCP project for embeddings",
        default=detected,
    ).ask() or ""
    location = questionary.text(
        "GCP region",
        default="us-central1",
    ).ask() or "us-central1"
    return {"GOOGLE_CLOUD_PROJECT": project, "GOOGLE_CLOUD_LOCATION": location}


def _step_budget() -> dict[str, str]:
    import questionary
    print()
    print("[4/5] Budget")
    print()
    budget = questionary.text(
        "Hard cap on cumulative LLM spend per crawl (USD)",
        default="100",
        validate=lambda v: v.replace(".", "", 1).isdigit() or "Number please",
    ).ask() or "100"
    workspace_root = questionary.text(
        "Where to store cloned repos",
        default=str(Path.home() / "servicescout-workspace"),
    ).ask()
    return {
        "BUDGET_USD": budget,
        "WORKSPACE_ROOT": workspace_root,
    }


def _step_confirm(workspace: dict[str, Any], env: dict[str, str]) -> None:
    print()
    print("[5/5] Review")
    print()
    print(f"  Orgs:           {', '.join(workspace['orgs']) or '(none)'}")
    print(f"  Excluded repos: {', '.join(workspace['excluded_repos']) or '(none)'}")
    print(f"  LLM:            {env['LLM_PROVIDER']} / {env['LLM_MODEL']} / effort={env['LLM_EFFORT']}")
    has_oai = "yes" if env.get("OPENAI_API_KEY") else "no (use CLI session)"
    has_ant = "yes" if env.get("ANTHROPIC_API_KEY") else "no (use CLI session)"
    if env["LLM_PROVIDER"] == "codex":
        print(f"  API key:        OpenAI {has_oai}")
    else:
        print(f"  API key:        Anthropic {has_ant}")
    print(f"  Embeddings:     {env['GOOGLE_CLOUD_PROJECT'] or 'disabled'} ({env['GOOGLE_CLOUD_LOCATION']})")
    print(f"  Budget:         ${env['BUDGET_USD']}")
    print(f"  Workspace:      {env['WORKSPACE_ROOT']}")
    print()


def _write_workspace(workspace: dict[str, Any]) -> None:
    WORKSPACE_PATH.write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")


def _write_env(env: dict[str, str]) -> None:
    template = ENV_EXAMPLE_PATH.read_text(encoding="utf-8") if ENV_EXAMPLE_PATH.exists() else ""
    lines = []
    keys_set: set[str] = set()
    for line in template.splitlines():
        if not line or line.startswith("#"):
            lines.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in env:
            lines.append(f"{key}={env[key]}")
            keys_set.add(key)
        else:
            lines.append(line)
    for key, value in env.items():
        if key not in keys_set:
            lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def _outro(env: dict[str, str]) -> None:
    print()
    print("─" * 60)
    print(" Setup complete.")
    print("─" * 60)
    print()
    print(" Next steps:")
    print()
    print("   # Start the persistent services")
    print("   docker compose up -d mcp dashboard")
    print()
    print("   # Run a crawl (one-shot)")
    print("   docker compose run --rm crawler")
    print()
    print(" Local dev (without Docker):")
    print()
    print("   .venv/bin/python crawler.py --root", env["WORKSPACE_ROOT"], "--discover --embed --reconcile")
    print("   .venv/bin/python dashboard.py")
    print("   .venv/bin/python mcp_server.py --transport stdio")
    print()
    print(" Connect a coding agent:")
    print()
    print("   claude mcp add --transport stdio servicescout -- python", str(HERE / "mcp_server.py"))
    print("   codex mcp add servicescout -- python", str(HERE / "mcp_server.py"))
    print()


def _check_only() -> int:
    if not WORKSPACE_PATH.exists():
        print(f"missing: {WORKSPACE_PATH}", file=sys.stderr)
        return 1
    if not ENV_PATH.exists():
        print(f"missing: {ENV_PATH}", file=sys.stderr)
        return 1
    try:
        ws = json.loads(WORKSPACE_PATH.read_text())
    except json.JSONDecodeError as exc:
        print(f"workspace.json invalid: {exc}", file=sys.stderr)
        return 1
    orgs = ws.get("orgs") or []
    if not orgs:
        print("workspace.json: no orgs configured", file=sys.stderr)
        return 1
    print(json.dumps({"workspace": str(WORKSPACE_PATH), "orgs": orgs, "env": str(ENV_PATH)}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate existing config, no prompts.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config without prompting.")
    args = parser.parse_args()

    if args.check:
        return _check_only()

    try:
        import questionary  # noqa: F401
    except ImportError:
        print("questionary is required. Install with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    _intro()
    import questionary
    if WORKSPACE_PATH.exists() and not args.force:
        action = questionary.select(
            f"{WORKSPACE_PATH.name} exists. What now?",
            choices=[
                {"name": "Keep existing and quit", "value": "keep"},
                {"name": "Reconfigure (overwrites workspace.json and .env)", "value": "reset"},
            ],
        ).ask()
        if action == "keep":
            print()
            print("Nothing changed.")
            return 0

    orgs, excluded = _step_orgs()
    llm = _step_llm()
    gcp = _step_gcp()
    budget = _step_budget()

    workspace = {"orgs": orgs, "excluded_repos": excluded}
    env = {**llm, **gcp, **budget, "HOST_UID": str(os.getuid()), "HOST_GID": str(os.getgid())}
    _step_confirm(workspace, env)
    if not questionary.confirm("Write these to disk?", default=True).ask():
        print("Cancelled.")
        return 0
    _write_workspace(workspace)
    _write_env(env)
    _outro(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
