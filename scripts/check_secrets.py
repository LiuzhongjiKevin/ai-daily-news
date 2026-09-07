"""Fail safely when configured secrets occur in tracked repository files."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SECRET_NAMES = (
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "DEEPSEEK_API_KEY",
    "MS_CLIENT_ID",
    "MS_TOKEN_KEY",
    "OUTLOOK_SENDER",
    "MAIL_TO",
    "AI_DAILY_STATE_TOKEN",
)


class SecretScanError(RuntimeError):
    """The tracked-file inventory could not be safely obtained."""


def tracked_paths() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SecretScanError from error
    return [Path(raw.decode("utf-8", "surrogateescape")) for raw in result.stdout.split(b"\0") if raw]


def read_tracked_bytes(path: Path) -> bytes:
    return path.read_bytes()


def main() -> int:
    secrets = {name: value.encode() for name in SECRET_NAMES if (value := os.environ.get(name))}
    try:
        paths = tracked_paths()
    except SecretScanError:
        print("Secret scan could not inspect tracked files.", file=sys.stderr)
        return 2
    matches: list[tuple[str, Path]] = []
    for path in paths:
        try:
            content = read_tracked_bytes(path)
        except OSError:
            print("Secret scan could not read a tracked file.", file=sys.stderr)
            return 2
        for name, value in secrets.items():
            if value in content:
                matches.append((name, path))
    for name, path in matches:
        print(f"Secret match: {name} in {path}")
    return 1 if matches else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
