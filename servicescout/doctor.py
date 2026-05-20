"""servicescout doctor — preflight diagnostics for a crawl/extraction run.

Verifies, for the selected provider, that:
  - the extractor can authenticate (CLI login *or* API key),
  - GitHub access is available (repo discovery + cloning),
  - the workspace and data mounts are usable,
  - embeddings are configured (optional).

Run it before a crawl so misconfiguration surfaces with a clear, actionable
report instead of an opaque failure mid-run. Designed for a fresh VM:

    docker compose run --rm doctor
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from servicescout.harnesses import provider_auth_status

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str


def _github_check(env: Mapping[str, str]) -> Check:
    if env.get("GITHUB_TOKEN") or env.get("GH_TOKEN"):
        return Check("github access", OK, "using GITHUB_TOKEN/GH_TOKEN from environment")
    if shutil.which("gh") is None:
        return Check("github access", FAIL, "gh CLI not found and no GITHUB_TOKEN set; cloning will fail")
    try:
        rc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30).returncode
    except Exception as exc:  # never hard-fail on a flaky probe
        return Check("github access", WARN, f"could not verify gh auth ({exc}); continuing")
    if rc == 0:
        return Check("github access", OK, "gh CLI authenticated")
    return Check("github access", FAIL, "gh CLI is not authenticated (`gh auth login`) and no GITHUB_TOKEN set")


def _dir_check(name: str, path: Path, *, write: bool) -> Check:
    if not path.exists():
        return Check(name, FAIL, f"{path} does not exist")
    if write and not os.access(path, os.W_OK):
        return Check(name, FAIL, f"{path} is not writable")
    if not write and not os.access(path, os.R_OK):
        return Check(name, FAIL, f"{path} is not readable")
    return Check(name, OK, f"{path} is {'writable' if write else 'readable'}")


def _embeddings_check(env: Mapping[str, str]) -> Check:
    if not env.get("GOOGLE_CLOUD_PROJECT"):
        return Check("embeddings", SKIP, "GOOGLE_CLOUD_PROJECT empty — lexical-only search")
    adc = env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if adc and Path(adc).exists():
        return Check("embeddings", OK, f"Vertex AI project set; ADC at {adc}")
    return Check("embeddings", WARN, "GOOGLE_CLOUD_PROJECT set but ADC file not found; embeddings may fail")


def gather_checks(
    provider: str,
    workspace_root: str,
    data_dir: str,
    *,
    env: Mapping[str, str] | None = None,
    check_github: bool = True,
) -> list[Check]:
    """Run all diagnostics and return their results (no I/O beyond probes)."""
    env = os.environ if env is None else env
    checks: list[Check] = []

    authed, detail = provider_auth_status(provider, env)
    checks.append(Check(f"extractor auth ({provider})", OK if authed else FAIL, detail))

    # The CODEX_API_KEY-vs-OPENAI_API_KEY billing footgun: codex can silently
    # switch to API-key billing when only OPENAI_API_KEY is present.
    if provider == "codex" and env.get("OPENAI_API_KEY") and not env.get("CODEX_API_KEY"):
        checks.append(
            Check(
                "codex billing",
                WARN,
                "OPENAI_API_KEY is set without CODEX_API_KEY — codex may silently use API-key "
                "billing. Set CODEX_API_KEY to make it explicit (also enables --ignore-user-config).",
            )
        )

    if check_github:
        checks.append(_github_check(env))
    checks.append(_dir_check("workspace mount", Path(workspace_root), write=False))
    checks.append(_dir_check("data dir", Path(data_dir), write=True))
    checks.append(_embeddings_check(env))
    return checks


def format_report(provider: str, checks: Sequence[Check]) -> str:
    label = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "skip"}
    lines = [f"ServiceScout doctor — provider={provider}", ""]
    for c in checks:
        lines.append(f"  [{label[c.status]}] {c.name:<22} {c.detail}")
    lines.append("")
    failed = sum(1 for c in checks if c.status == FAIL)
    if failed:
        lines.append(f"FAIL: {failed} required check(s) failed. Fix the items above before crawling.")
    else:
        lines.append("OK: ready to crawl.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=("codex", "claude"), default=os.environ.get("LLM_PROVIDER", "codex"))
    parser.add_argument("--workspace-root", default=os.environ.get("WORKSPACE_ROOT", "/workspace"))
    parser.add_argument("--data-dir", default=os.environ.get("SERVICESCOUT_DATA_DIR", "/data"))
    parser.add_argument(
        "--no-github",
        action="store_true",
        help="Skip the GitHub access check (e.g. demo/eval runs that don't clone).",
    )
    args = parser.parse_args(argv)

    checks = gather_checks(
        args.provider,
        args.workspace_root,
        args.data_dir,
        check_github=not args.no_github,
    )
    print(format_report(args.provider, checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
