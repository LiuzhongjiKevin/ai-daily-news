from __future__ import annotations

import base64
import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_remote_default.py"
AUTHORIZED_SHA = "a" * 40


def load_module() -> object:
    spec = importlib.util.spec_from_file_location("check_remote_default", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_default_requires_exact_authorized_tip() -> None:
    """Would catch a mutable default branch advancing before the first state mutation."""
    module = load_module()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, stdout=f"{AUTHORIZED_SHA}\trefs/heads/main\n", stderr=""
        )

    assert (
        module.remote_default_matches(
            "main", AUTHORIZED_SHA, github_token="built-in-read-token", run=runner
        )
        is True
    )
    assert calls[0][0] == [
        "git",
        "ls-remote",
        "--exit-code",
        "origin",
        "refs/heads/main",
    ]
    git_environment = calls[0][1]["env"]
    assert isinstance(git_environment, dict)
    assert git_environment["GIT_CONFIG_COUNT"] == "1"
    assert git_environment["GIT_CONFIG_KEY_0"] == "http.extraheader"
    expected = base64.b64encode(b"x-access-token:built-in-read-token").decode("ascii")
    assert git_environment["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert git_environment.get("GITHUB_TOKEN") is None


@pytest.mark.parametrize(
    ("branch", "authorized_sha", "remote_output"),
    [
        ("main", AUTHORIZED_SHA, f"{'b' * 40}\trefs/heads/main\n"),
        ("main", AUTHORIZED_SHA, ""),
        ("main", AUTHORIZED_SHA, f"{AUTHORIZED_SHA}\trefs/heads/main\nextra\n"),
        ("main", "not-a-sha", f"{AUTHORIZED_SHA}\trefs/heads/main\n"),
        ("../main", AUTHORIZED_SHA, f"{AUTHORIZED_SHA}\trefs/heads/../main\n"),
        ("main", AUTHORIZED_SHA, f"{AUTHORIZED_SHA}\trefs/heads/other\n"),
    ],
)
def test_remote_default_fails_closed_on_mismatch_or_malformed_metadata(
    branch: str, authorized_sha: str, remote_output: str
) -> None:
    """Would catch ambiguous or injected remote-ref metadata being accepted."""
    module = load_module()

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=remote_output, stderr="provider detail")

    assert module.remote_default_matches(branch, authorized_sha, run=runner) is False


def test_remote_default_fails_closed_without_leaking_provider_errors() -> None:
    """Would catch remote command failures being treated as a match or exposing provider output."""
    module = load_module()

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(2, args, stderr="credential-bearing provider detail")

    assert module.remote_default_matches("main", AUTHORIZED_SHA, run=runner) is False
