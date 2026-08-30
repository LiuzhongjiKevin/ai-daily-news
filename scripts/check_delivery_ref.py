"""Emit a fail-closed, case-sensitive default-branch delivery authorization."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> int:
    output_path = os.environ.get("GITHUB_OUTPUT")
    default_branch = os.environ.get("DEFAULT_BRANCH")
    github_ref = os.environ.get("GITHUB_REF")
    github_ref_type = os.environ.get("GITHUB_REF_TYPE")
    trusted = (
        bool(default_branch)
        and github_ref_type == "branch"
        and github_ref == f"refs/heads/{default_branch}"
    )
    if not output_path:
        return 2
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"trusted={str(trusted).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
