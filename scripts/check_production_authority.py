"""Authorize production only from default-branch code and trusted GitHub events."""

from __future__ import annotations

import os
import re
from pathlib import Path

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_WORKFLOW_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}")


def _is_exact_default_branch(default_branch: str) -> bool:
    return (
        bool(default_branch)
        and os.environ.get("GITHUB_REF_TYPE") == "branch"
        and os.environ.get("GITHUB_REF") == f"refs/heads/{default_branch}"
    )


def _valid_sha(value: str) -> bool:
    return _SHA_PATTERN.fullmatch(value) is not None


def _trusted_workflow_identity() -> bool:
    expected_name = os.environ.get("EXPECTED_TRIGGER_WORKFLOW_NAME", "")
    expected_path = os.environ.get("EXPECTED_TRIGGER_WORKFLOW_PATH", "")
    return (
        bool(expected_name)
        and bool(expected_path)
        and os.environ.get("TRIGGER_WORKFLOW_NAME") == expected_name
        and os.environ.get("TRIGGER_WORKFLOW_PATH") == expected_path
        and _WORKFLOW_ID_PATTERN.fullmatch(os.environ.get("TRIGGER_WORKFLOW_ID", "")) is not None
    )


def _trusted(default_branch: str) -> bool:
    if not _is_exact_default_branch(default_branch):
        return False
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    github_sha = os.environ.get("GITHUB_SHA", "")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not _valid_sha(github_sha) or _REPOSITORY_PATTERN.fullmatch(github_repository) is None:
        return False
    if event_name == "schedule":
        return True
    if event_name != "workflow_run":
        return False
    return (
        _trusted_workflow_identity()
        and (
            os.environ.get("TRIGGER_EVENT") == "repository_dispatch"
            or (
                os.environ.get("TRIGGER_EVENT") == "workflow_dispatch"
                and os.environ.get("EXPECTED_TRIGGER_WORKFLOW_NAME") == "AI Daily Request"
                and os.environ.get("EXPECTED_TRIGGER_WORKFLOW_PATH") == ".github/workflows/request.yml"
            )
        )
        and os.environ.get("TRIGGER_HEAD_BRANCH") == default_branch
        and os.environ.get("TRIGGER_HEAD_SHA") == github_sha
        and os.environ.get("TRIGGER_HEAD_REPOSITORY") == github_repository
        and os.environ.get("TRIGGER_CONCLUSION") == "success"
    )


def main() -> int:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return 2
    trusted = _trusted(os.environ.get("DEFAULT_BRANCH", ""))
    authorized_sha = os.environ.get("GITHUB_SHA", "") if trusted else ""
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"trusted={str(trusted).lower()}\n")
        output.write(f"authorized_sha={authorized_sha}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
