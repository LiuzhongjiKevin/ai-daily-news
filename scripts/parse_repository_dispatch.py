"""Strictly parse default-branch repository-dispatch control payloads."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

_ACTIONS = {
    "daily": "ai-daily-request",
    "resolve": "ai-daily-resolve",
    "sources": "ai-daily-validate-sources",
}
_DAILY_KEYS = frozenset({"target_ref", "mode", "send", "force"})
_RESOLVE_KEYS = frozenset(
    {"target_ref", "date", "resolution", "message_id", "confirm"}
)
_SOURCE_KEYS = frozenset({"minimum_success"})
_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}"
)
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,255}")
_INVALID_REF_CHARS = frozenset(" ~^:?*[\\")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _load_event(path_value: str) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    try:
        if not path.is_file() or path.stat().st_size > 1_048_576:
            return None
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_ref_name(value: str) -> bool:
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


def _target_ref(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    for prefix, ref_type in (("refs/heads/", "branch"), ("refs/tags/", "tag")):
        if value.startswith(prefix) and _valid_ref_name(value.removeprefix(prefix)):
            return value, ref_type
    return None


def _daily(payload: dict[str, Any]) -> dict[str, str] | None:
    target = _target_ref(payload.get("target_ref"))
    mode = payload.get("mode")
    send = payload.get("send")
    force = payload.get("force")
    if (
        set(payload) != _DAILY_KEYS
        or target is None
        or mode not in {"full", "economy", "off"}
        or not isinstance(mode, str)
        or not isinstance(send, bool)
        or not isinstance(force, bool)
    ):
        return None
    target_ref, ref_type = target
    return {
        "target_ref": target_ref,
        "ref_type": ref_type,
        "mode": mode,
        "send": str(send).lower(),
        "force": str(force).lower(),
    }


def _resolve(payload: dict[str, Any], default_branch: str) -> dict[str, str] | None:
    target = _target_ref(payload.get("target_ref"))
    local_date = payload.get("date")
    resolution = payload.get("resolution")
    message_id = payload.get("message_id")
    confirm = payload.get("confirm")
    if (
        set(payload) != _RESOLVE_KEYS
        or target != (f"refs/heads/{default_branch}", "branch")
        or not isinstance(local_date, str)
        or not isinstance(resolution, str)
        or not isinstance(message_id, str)
        or confirm is not True
    ):
        return None
    try:
        if date.fromisoformat(local_date).isoformat() != local_date:
            return None
    except ValueError:
        return None
    if resolution == "retry" and message_id:
        return None
    if resolution == "sent" and _MESSAGE_ID.fullmatch(message_id) is None:
        return None
    if resolution not in {"retry", "sent"}:
        return None
    return {
        "target_ref": target[0],
        "date": local_date,
        "resolution": resolution,
        "message_id": message_id,
        "confirm": "true",
    }


def _sources(payload: dict[str, Any]) -> dict[str, str] | None:
    threshold = payload.get("minimum_success")
    if (
        set(payload) != _SOURCE_KEYS
        or isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not 0 <= threshold <= 100
    ):
        return None
    return {"minimum_success": str(threshold)}


def _validated_values(kind: str, event: dict[str, Any]) -> dict[str, str] | None:
    repository = event.get("repository")
    payload = event.get("client_payload")
    github_repository = os.environ.get("GITHUB_REPOSITORY", "")
    default_branch = os.environ.get("DEFAULT_BRANCH", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs")
        if (
            kind != "daily"
            or os.environ.get("GITHUB_REF") != f"refs/heads/{default_branch}"
            or not isinstance(inputs, dict)
            or set(inputs) != {"send"}
            or not isinstance(inputs["send"], (str, bool))
            or str(inputs["send"]).lower() not in {"true", "false"}
        ):
            return None
        payload = {"target_ref": f"refs/heads/{default_branch}", "mode": "off",
                   "send": str(inputs["send"]).lower() == "true", "force": False}
    if (
        event_name not in {"repository_dispatch", "workflow_dispatch"}
        or (event_name == "repository_dispatch" and event.get("action") != _ACTIONS[kind])
        or not isinstance(repository, dict)
        or not isinstance(payload, dict)
        or _REPOSITORY_PATTERN.fullmatch(github_repository) is None
        or repository.get("full_name") != github_repository
        or repository.get("default_branch") != default_branch
        or not default_branch
    ):
        return None
    if kind == "daily":
        return _daily(payload)
    if kind == "resolve":
        return _resolve(payload, default_branch)
    return _sources(payload)


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    if len(arguments) != 1 or arguments[0] not in _ACTIONS:
        return 2
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    event = _load_event(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not output_path or event is None:
        return 2
    values = _validated_values(arguments[0], event)
    if values is None:
        return 2
    try:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.writelines(f"{name}={value}\n" for name, value in values.items())
    except OSError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
