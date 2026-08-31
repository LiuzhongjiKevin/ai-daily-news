"""Parse allowlisted production request fields from trusted workflow-run metadata."""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

_DAILY = re.compile(
    r"ai-daily-request\|mode=(full|economy|off)\|send=(true|false)\|force=(true|false)"
)
_RESOLVE = re.compile(
    r"ai-daily-resolve\|date=(\d{4}-\d{2}-\d{2})\|resolution=(retry|sent)\|"
    r"message_id=([A-Za-z0-9._:-]*)\|confirm=(true|false)"
)
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}")


def _trusted_branch_title(title: str, default_branch: str) -> str | None:
    if not default_branch:
        return None
    suffix = f"|ref_type=branch|ref=refs/heads/{default_branch}"
    return title[: -len(suffix)] if title.endswith(suffix) else None


def _daily(title: str, default_branch: str) -> dict[str, str] | None:
    request = _trusted_branch_title(title, default_branch)
    if request is None:
        return None
    match = _DAILY.fullmatch(request)
    if match is None:
        return None
    mode, send, force = match.groups()
    return {"mode": mode, "send": send, "force": force}


def _resolve(title: str, default_branch: str) -> dict[str, str] | None:
    request = _trusted_branch_title(title, default_branch)
    if request is None:
        return None
    match = _RESOLVE.fullmatch(request)
    if match is None:
        return None
    local_date, resolution, message_id, confirm = match.groups()
    try:
        if date.fromisoformat(local_date).isoformat() != local_date:
            return None
    except ValueError:
        return None
    if confirm != "true":
        return None
    if resolution == "retry" and message_id:
        return None
    if resolution == "sent" and _MESSAGE_ID.fullmatch(message_id) is None:
        return None
    return {
        "date": local_date,
        "resolution": resolution,
        "message_id": message_id,
        "confirm": confirm,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1 or arguments[0] not in {"daily", "resolve"}:
        return 2
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return 2
    title = os.environ.get("REQUEST_TITLE", "")
    default_branch = os.environ.get("DEFAULT_BRANCH", "")
    values = (
        _daily(title, default_branch)
        if arguments[0] == "daily"
        else _resolve(title, default_branch)
    )
    if values is None:
        return 2
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.writelines(f"{name}={value}\n" for name, value in values.items())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
