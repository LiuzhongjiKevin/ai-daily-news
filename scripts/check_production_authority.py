"""Authorize production only from default-branch code and trusted GitHub events."""

from __future__ import annotations

import os
from pathlib import Path


def _is_exact_default_branch(default_branch: str) -> bool:
    return (
        bool(default_branch)
        and os.environ.get("GITHUB_REF_TYPE") == "branch"
        and os.environ.get("GITHUB_REF") == f"refs/heads/{default_branch}"
    )


def _trusted(default_branch: str) -> bool:
    if not _is_exact_default_branch(default_branch):
        return False
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if event_name == "schedule":
        return True
    if event_name != "workflow_run":
        return False
    return (
        os.environ.get("TRIGGER_EVENT") == "workflow_dispatch"
        and os.environ.get("TRIGGER_HEAD_BRANCH") == default_branch
        and bool(os.environ.get("GITHUB_SHA"))
        and os.environ.get("TRIGGER_HEAD_SHA") == os.environ.get("GITHUB_SHA")
        and os.environ.get("TRIGGER_HEAD_REPOSITORY")
        == os.environ.get("GITHUB_REPOSITORY")
        and os.environ.get("TRIGGER_CONCLUSION") == "success"
    )


def main() -> int:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return 2
    trusted = _trusted(os.environ.get("DEFAULT_BRANCH", ""))
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"trusted={str(trusted).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
