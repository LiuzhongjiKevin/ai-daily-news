from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "push_production_state.py"


def load_module() -> object:
    spec = importlib.util.spec_from_file_location("push_production_state", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_state_push_uses_only_ephemeral_process_auth_without_mutating_environment() -> None:
    """Would catch the state PAT being persisted in git config or surviving into later steps."""
    module = load_module()
    calls: list[tuple[list[str], dict[str, object]]] = []
    before = dict(os.environ)

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    assert module.push_production_state("main", "state-token-value", run=runner) is True
    assert os.environ == before
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["git", "push", "origin", "HEAD:refs/heads/main"]
    git_environment = kwargs["env"]
    assert isinstance(git_environment, dict)
    assert git_environment["GIT_CONFIG_COUNT"] == "1"
    assert git_environment["GIT_CONFIG_KEY_0"] == "http.extraheader"
    expected = base64.b64encode(b"x-access-token:state-token-value").decode("ascii")
    assert git_environment["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert git_environment["GIT_TERMINAL_PROMPT"] == "0"
    assert git_environment.get("AI_DAILY_STATE_TOKEN") is None
    assert "state-token-value" not in " ".join(args)
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True


@pytest.mark.parametrize(
    ("branch", "token"),
    [("../main", "token"), ("main", ""), ("main", "line\nbreak")],
)
def test_state_push_rejects_invalid_branch_or_token_without_invoking_git(
    branch: str, token: str
) -> None:
    """Would catch malformed metadata or an absent credential reaching git."""
    module = load_module()
    called = False

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    assert module.push_production_state(branch, token, run=runner) is False
    assert called is False


def test_state_push_fails_closed_without_exposing_provider_output() -> None:
    """Would catch a rejected/non-fast-forward push passing or printing provider detail."""
    module = load_module()

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 1, stdout="provider-token-detail", stderr="provider-token-detail"
        )

    assert module.push_production_state("main", "state-token-value", run=runner) is False
