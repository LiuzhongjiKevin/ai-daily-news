"""Push production state with a step-scoped credential that is never persisted."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from collections.abc import Callable

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
        and not any(
            character in _INVALID_REF_CHARS or ord(character) < 32
            for character in value
        )
        and all(component and not component.startswith(".") for component in components)
        and all(not component.endswith(".lock") for component in components)
    )


def _git_environment(token: str) -> dict[str, str] | None:
    if not token or len(token) > 1024 or any(ord(character) < 33 for character in token):
        return None
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    environment = dict(os.environ)
    environment.pop("AI_DAILY_STATE_TOKEN", None)
    environment.pop("GITHUB_TOKEN", None)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def push_production_state(
    default_branch: str,
    token: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    environment = _git_environment(token)
    if not _valid_branch(default_branch) or environment is None:
        return False
    command = ["git", "push", "origin", f"HEAD:refs/heads/{default_branch}"]
    try:
        completed = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def main() -> int:
    if push_production_state(
        os.environ.get("DEFAULT_BRANCH", ""),
        os.environ.get("AI_DAILY_STATE_TOKEN", ""),
    ):
        return 0
    print("Production state push failed safely.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
