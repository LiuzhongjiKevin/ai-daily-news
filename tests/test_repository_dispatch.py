from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "parse_repository_dispatch.py"


def run_parser(
    tmp_path: Path,
    kind: str,
    action: str,
    payload: object,
    *,
    repository: object | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    event = tmp_path / "event.json"
    output = tmp_path / "github-output.txt"
    output.unlink(missing_ok=True)
    event.write_text(
        json.dumps(
            {
                "action": action,
                "branch": "main",
                "client_payload": payload,
                "repository": repository
                if repository is not None
                else {"full_name": "owner/repository", "default_branch": "main"},
                "sender": {"login": "operator"},
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), kind],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "repository_dispatch",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "owner/repository",
            "DEFAULT_BRANCH": "main",
        },
        timeout=5,
    )
    return completed, output


@pytest.mark.parametrize(
    ("target_ref", "ref_type"),
    [("refs/heads/feature/safe-preview", "branch"), ("refs/tags/review-v1", "tag")],
)
def test_daily_dispatch_accepts_strict_branch_or_tag_preview_payload(
    tmp_path: Path, target_ref: str, ref_type: str
) -> None:
    """Would catch the trusted request workflow being unable to safely select an untrusted ref."""
    completed, output = run_parser(
        tmp_path,
        "daily",
        "ai-daily-request",
        {"target_ref": target_ref, "mode": "off", "send": False, "force": False},
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text("utf-8").splitlines() == [
        f"target_ref={target_ref}",
        f"ref_type={ref_type}",
        "mode=off",
        "send=false",
        "force=false",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "off", "send": False, "force": False},
        {
            "target_ref": "refs/heads/main",
            "mode": "off",
            "send": False,
            "force": False,
            "unknown": "rejected",
        },
        {"target_ref": "refs/heads/main", "mode": "off", "send": "false", "force": False},
        {"target_ref": "refs/heads/main", "mode": "unsafe", "send": False, "force": False},
        {"target_ref": "main", "mode": "off", "send": False, "force": False},
        {"target_ref": "refs/pull/7/head", "mode": "off", "send": False, "force": False},
        {"target_ref": "refs/heads/../main", "mode": "off", "send": False, "force": False},
        {"target_ref": "refs/tags/v1\nmode=full", "mode": "off", "send": False, "force": False},
    ],
)
def test_daily_dispatch_rejects_missing_unknown_typed_or_unsafe_payloads(
    tmp_path: Path, payload: object
) -> None:
    """Would catch malformed dispatch data reaching a checkout or trusted production parser."""
    completed, output = run_parser(tmp_path, "daily", "ai-daily-request", payload)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not output.exists() or output.read_text("utf-8") == ""


@pytest.mark.parametrize(
    ("action", "repository"),
    [
        ("wrong-event", {"full_name": "owner/repository", "default_branch": "main"}),
        ("ai-daily-request", {}),
        (
            "ai-daily-request",
            {"full_name": "other/repository", "default_branch": "main"},
        ),
        (
            "ai-daily-request",
            {"full_name": "owner/repository", "default_branch": "other"},
        ),
    ],
)
def test_dispatch_rejects_wrong_event_or_repository_identity(
    tmp_path: Path, action: str, repository: object
) -> None:
    """Would catch a different event or malformed repository envelope authorizing a request."""
    completed, output = run_parser(
        tmp_path,
        "daily",
        action,
        {"target_ref": "refs/heads/main", "mode": "off", "send": False, "force": False},
        repository=repository,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not output.exists() or output.read_text("utf-8") == ""


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "target_ref": "refs/heads/main",
                "date": "2026-08-24",
                "resolution": "retry",
                "message_id": "",
                "confirm": True,
            },
            [
                "target_ref=refs/heads/main",
                "date=2026-08-24",
                "resolution=retry",
                "message_id=",
                "confirm=true",
            ],
        ),
        (
            {
                "target_ref": "refs/heads/main",
                "date": "2026-08-24",
                "resolution": "sent",
                "message_id": "mailbox-confirmed-2026-08-24",
                "confirm": True,
            },
            [
                "target_ref=refs/heads/main",
                "date=2026-08-24",
                "resolution=sent",
                "message_id=mailbox-confirmed-2026-08-24",
                "confirm=true",
            ],
        ),
    ],
)
def test_resolution_dispatch_accepts_only_confirmed_default_branch_requests(
    tmp_path: Path, payload: dict[str, object], expected: list[str]
) -> None:
    """Would catch a valid terminal-owner recovery request being rejected or rewritten."""
    completed, output = run_parser(
        tmp_path, "resolve", "ai-daily-resolve", payload
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text("utf-8").splitlines() == expected


@pytest.mark.parametrize(
    "payload",
    [
        {
            "target_ref": "refs/heads/feature/recovery",
            "date": "2026-08-24",
            "resolution": "retry",
            "message_id": "",
            "confirm": True,
        },
        {
            "target_ref": "refs/heads/main",
            "date": "2026-8-24",
            "resolution": "retry",
            "message_id": "",
            "confirm": True,
        },
        {
            "target_ref": "refs/heads/main",
            "date": "2026-08-24",
            "resolution": "unknown",
            "message_id": "",
            "confirm": True,
        },
        {
            "target_ref": "refs/heads/main",
            "date": "2026-08-24",
            "resolution": "retry",
            "message_id": "unexpected-id",
            "confirm": True,
        },
        {
            "target_ref": "refs/heads/main",
            "date": "2026-08-24",
            "resolution": "sent",
            "message_id": "unsafe@email",
            "confirm": True,
        },
        {
            "target_ref": "refs/heads/main",
            "date": "2026-08-24",
            "resolution": "retry",
            "message_id": "",
            "confirm": False,
        },
    ],
)
def test_resolution_dispatch_fails_closed_on_unsafe_or_unconfirmed_payload(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """Would catch unsafe recovery metadata clearing or marking a delivery intent."""
    completed, output = run_parser(
        tmp_path, "resolve", "ai-daily-resolve", payload
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not output.exists() or output.read_text("utf-8") == ""


def test_source_validation_dispatch_requires_a_bounded_integer_threshold(
    tmp_path: Path,
) -> None:
    """Would catch a string or out-of-range threshold entering a trusted command."""
    completed, output = run_parser(
        tmp_path, "sources", "ai-daily-validate-sources", {"minimum_success": 80}
    )
    assert completed.returncode == 0
    assert output.read_text("utf-8") == "minimum_success=80\n"

    for payload in (
        {},
        {"minimum_success": "80"},
        {"minimum_success": True},
        {"minimum_success": -1},
        {"minimum_success": 101},
        {"minimum_success": 80, "unknown": 1},
    ):
        completed, output = run_parser(
            tmp_path, "sources", "ai-daily-validate-sources", payload
        )
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == ""
        assert not output.exists() or output.read_text("utf-8") == ""
