"""Fail closed unless the remote default branch still equals the authorized commit."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from collections.abc import Callable

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_INVALID_REF_CHARS = frozenset(" ~^:?*[\\")


def _valid_branch(value: str) -> bool:
    components = value.split("/")
    return (
        bool(value)
        and len(value) <= 255
        and value != "@"
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", "."))
        and "//" not in value
        and ".." not in value
        and "@{" not in value
        and not any(character in _INVALID_REF_CHARS or ord(character) < 32 for character in value)
        and all(component and not component.startswith(".") for component in components)
        and all(not component.endswith(".lock") for component in components)
    )


def remote_default_matches(
    default_branch: str,
    authorized_sha: str,
    *,
    github_token: str = "",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if (
        not _valid_branch(default_branch)
        or _SHA_PATTERN.fullmatch(authorized_sha) is None
        or not github_token
        or len(github_token) > 1024
        or any(ord(character) < 33 for character in github_token)
    ):
        return False
    expected_ref = f"refs/heads/{default_branch}"
    command = ["git", "ls-remote", "--exit-code", "origin", expected_ref]
    encoded = base64.b64encode(f"x-access-token:{github_token}".encode()).decode("ascii")
    environment = dict(os.environ)
    environment.pop("GITHUB_TOKEN", None)
    environment.pop("AI_DAILY_STATE_TOKEN", None)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        return False
    fields = lines[0].split("\t")
    return fields == [authorized_sha, expected_ref]


def main() -> int:
    if remote_default_matches(
        os.environ.get("DEFAULT_BRANCH", ""),
        os.environ.get("AUTHORIZED_SHA", ""),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
    ):
        return 0
    print("Remote default branch no longer matches the authorized commit.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
