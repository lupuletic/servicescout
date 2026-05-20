"""Resolve isolated eval workspace paths.

Sock Shop keeps its historical paths for compatibility:
  evals/workspace, evals/data, evals/questions.yaml

Additional public benchmarks live under:
  evals/workspaces/<name>/
"""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class EvalWorkspace:
    name: str
    root: Path
    workspace_json: Path
    lock_json: Path
    clone_root: Path
    data_dir: Path
    extraction_dir: Path
    orgs_config: Path
    questions: Path
    runs_dir: Path


def resolve_workspace(name: str | None) -> EvalWorkspace:
    slug = (name or "sock-shop").strip() or "sock-shop"
    if slug in {"sock-shop", "socks-shop", "default"}:
        return EvalWorkspace(
            name="sock-shop",
            root=HERE,
            workspace_json=HERE / "workspace.json",
            lock_json=HERE / "workspace.lock.json",
            clone_root=HERE / "workspace",
            data_dir=HERE / "data",
            extraction_dir=HERE / "data" / "extractions",
            orgs_config=HERE / "workspace_orgs.json",
            questions=HERE / "questions.yaml",
            runs_dir=HERE / "runs",
        )

    root = HERE / "workspaces" / slug
    return EvalWorkspace(
        name=slug,
        root=root,
        workspace_json=root / "workspace.json",
        lock_json=root / "workspace.lock.json",
        clone_root=root / "workspace",
        data_dir=root / "data",
        extraction_dir=root / "data" / "extractions",
        orgs_config=root / "workspace_orgs.json",
        questions=root / "questions.yaml",
        runs_dir=root / "runs",
    )


def _serialise(ws: EvalWorkspace) -> dict[str, str]:
    return {key: str(value) for key, value in asdict(ws).items()}


def _shell(ws: EvalWorkspace) -> str:
    return "\n".join(
        f"SS_{key.upper()}={shlex.quote(str(value))}"
        for key, value in _serialise(ws).items()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default="sock-shop", help="Eval workspace slug.")
    parser.add_argument("--shell", action="store_true", help="Emit shell assignments.")
    args = parser.parse_args()

    ws = resolve_workspace(args.workspace)
    if args.shell:
        print(_shell(ws))
    else:
        print(json.dumps(_serialise(ws), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
