from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
AUTHORIZED_SHA = "a" * 40


def load_workflow(name: str) -> dict[object, object]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_ci_is_offline_and_uses_reviewed_action_pins() -> None:
    """Would catch CI drifting to live source tests or a mutable action reference."""
    workflow = load_workflow("ci.yml")
    steps = workflow["jobs"]["test"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
    ]
    assert any('pytest -m "not live"' in step.get("run", "") for step in steps)


def test_daily_uses_all_reviewed_action_pins() -> None:
    """Would catch a mutable artifact action reference bypassing the reviewed supply-chain pin."""
    workflow = load_workflow("daily.yml")
    daily_steps = workflow["jobs"]["daily"]["steps"]
    preview_steps = workflow["jobs"]["cost_preview"]["steps"]
    request_steps = load_workflow("request.yml")["jobs"]["safe_preview"]["steps"]
    assert [step["uses"] for step in daily_steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]
    assert [step["uses"] for step in preview_steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]
    assert [step["uses"] for step in request_steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]


def test_workflows_install_exact_locked_dependency_sets_before_editable_package() -> None:
    """Would catch Actions resolving broad package ranges instead of the reviewed lock files."""
    ci_steps = load_workflow("ci.yml")["jobs"]["test"]["steps"]
    daily_steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    cost_preview_steps = load_workflow("daily.yml")["jobs"]["cost_preview"]["steps"]
    request_steps = load_workflow("request.yml")["jobs"]["safe_preview"]["steps"]
    ci_commands = "\n".join(step.get("run", "") for step in ci_steps)
    daily_commands = "\n".join(step.get("run", "") for step in daily_steps)
    cost_preview_commands = "\n".join(step.get("run", "") for step in cost_preview_steps)
    request_commands = "\n".join(step.get("run", "") for step in request_steps)

    assert "pip install --requirement requirements-dev.lock" in ci_commands
    assert 'pip install --no-deps --no-build-isolation -e ".[dev]"' in ci_commands
    assert "pip install --requirement requirements-prod.lock" in daily_commands
    assert 'pip install --no-deps --no-build-isolation -e .' in daily_commands
    assert ".[dev]" not in daily_commands
    for commands in (cost_preview_commands, request_commands):
        assert "pip install --requirement requirements-prod.lock" in commands
        assert 'pip install --no-deps --no-build-isolation -e .' in commands
        assert ".[dev]" not in commands

    for lock_name in ("requirements-prod.lock", "requirements-dev.lock"):
        lines = (ROOT / lock_name).read_text(encoding="utf-8").splitlines()
        assert lines
        assert all(not line or line.startswith(("#", "-r ")) or "==" in line for line in lines)


def test_daily_schedule_manual_inputs_and_state_commit_are_restricted() -> None:
    """Would catch scheduled runs accepting dispatch inputs or committing arbitrary workspace files."""
    workflow = load_workflow("daily.yml")
    trigger = workflow[True]
    assert trigger["schedule"] == [
        {"cron": "47 6 * * *", "timezone": "Asia/Shanghai"},
        {"cron": "22 7 * * *", "timezone": "Asia/Shanghai"},
    ]
    assert trigger["workflow_run"] == {
        "workflows": ["AI Daily Request"],
        "types": ["completed"],
    }
    assert "workflow_dispatch" not in trigger
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = workflow["jobs"]["daily"]["steps"]
    command = next(step["run"] for step in steps if step.get("name") == "Run daily digest")
    assert workflow["jobs"]["authorize"]["outputs"]["mode"] == "${{ steps.request.outputs.mode }}"
    assert 'if [ "${{ github.event_name }}" = "workflow_run" ]; then' in command
    assert 'mode_arg="--ai-mode ${{ needs.authorize.outputs.mode }}"' in command
    assert "${mode_arg}" in command
    assert "needs.authorize.outputs.mode" in command
    assert "ai-daily run --send" in command
    commits = [
        step["run"]
        for step in steps
        if step.get("name")
        in {"Commit reserved intent", "Commit unattempted cleanup", "Commit accepted state"}
    ]
    assert len(commits) == 3
    assert "git add -- data/ digests/ data/microsoft-token.enc" in commits[-1]
    assert all("git add -A" not in commit for commit in commits)
    assert all("git pull" not in commit for commit in commits)
    assert all("git push" not in commit for commit in commits)
    assert all("--force" not in commit for commit in commits)
    assert "Refusing to send without a new local reservation." in commits[0]


def test_scheduled_delivery_requires_explicit_repository_variable_gate() -> None:
    """Would catch a default-branch push activating scheduled sends before cost review."""
    workflow = load_workflow("daily.yml")
    gate = workflow["jobs"]["daily"]["if"]
    assert "github.event_name == 'schedule'" in gate
    assert "vars.AI_DAILY_SCHEDULE_ENABLED == 'true'" in gate
    assert "needs.authorize.outputs.send == 'true'" in gate


@pytest.mark.parametrize(
    (
        "case",
        "event_name",
        "ref",
        "ref_type",
        "trigger_event",
        "trigger_branch",
        "trigger_repository",
        "trusted",
    ),
    [
        ("scheduled default", "schedule", "refs/heads/main", "branch", "", "", "", True),
        (
            "manual default",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "repository_dispatch",
            "main",
            "owner/repository",
            True,
        ),
        (
            "ordinary feature",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "repository_dispatch",
            "feature/safe-preview",
            "owner/repository",
            False,
        ),
        (
            "case collision",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "repository_dispatch",
            "Main",
            "owner/repository",
            False,
        ),
        (
            "same-named tag",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "repository_dispatch",
            "",
            "owner/repository",
            False,
        ),
        (
            "foreign repository",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "repository_dispatch",
            "main",
            "other/repository",
            False,
        ),
        (
            "wrong trigger event",
            "workflow_run",
            "refs/heads/main",
            "branch",
            "push",
            "main",
            "owner/repository",
            False,
        ),
    ],
)
def test_production_authority_gate_uses_trusted_default_branch_event_metadata(
    tmp_path: Path,
    case: str,
    event_name: str,
    ref: str,
    ref_type: str,
    trigger_event: str,
    trigger_branch: str,
    trigger_repository: str,
    trusted: bool,
) -> None:
    """Would catch branch-authored verifier output or case-insensitive refs authorizing production."""
    output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REF": ref,
        "GITHUB_REF_TYPE": ref_type,
        "GITHUB_SHA": AUTHORIZED_SHA,
        "DEFAULT_BRANCH": "main",
        "GITHUB_REPOSITORY": "owner/repository",
        "TRIGGER_EVENT": trigger_event,
        "TRIGGER_HEAD_BRANCH": trigger_branch,
        "TRIGGER_HEAD_SHA": AUTHORIZED_SHA,
        "TRIGGER_HEAD_REPOSITORY": trigger_repository,
        "TRIGGER_CONCLUSION": "success",
        "EXPECTED_TRIGGER_WORKFLOW_NAME": "AI Daily Request",
        "EXPECTED_TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
        "TRIGGER_WORKFLOW_NAME": "AI Daily Request",
        "TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
        "TRIGGER_WORKFLOW_ID": "123456",
        "GITHUB_OUTPUT": str(output),
    }

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_production_authority.py")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert completed.returncode == 0, case
    assert completed.stdout == ""
    assert completed.stderr == ""
    expected_sha = AUTHORIZED_SHA if trusted else ""
    assert output.read_text(encoding="utf-8") == (
        f"trusted={str(trusted).lower()}\nauthorized_sha={expected_sha}\n"
    )


def test_production_authority_rejects_same_named_tag_or_stale_ref_at_another_sha(
    tmp_path: Path,
) -> None:
    """Would catch a tag reported as head_branch=main authorizing production code."""
    output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_SHA": AUTHORIZED_SHA,
        "DEFAULT_BRANCH": "main",
        "GITHUB_REPOSITORY": "owner/repository",
        "TRIGGER_EVENT": "repository_dispatch",
        "TRIGGER_HEAD_BRANCH": "main",
        "TRIGGER_HEAD_REPOSITORY": "owner/repository",
        "TRIGGER_HEAD_SHA": "b" * 40,
        "TRIGGER_CONCLUSION": "success",
        "EXPECTED_TRIGGER_WORKFLOW_NAME": "AI Daily Request",
        "EXPECTED_TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
        "TRIGGER_WORKFLOW_NAME": "AI Daily Request",
        "TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
        "TRIGGER_WORKFLOW_ID": "123456",
        "GITHUB_OUTPUT": str(output),
    }

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_production_authority.py")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text(encoding="utf-8") == "trusted=false\nauthorized_sha=\n"


@pytest.mark.parametrize(
    ("name", "path", "workflow_id"),
    [
        ("Wrong Request", ".github/workflows/request.yml", "123456"),
        ("AI Daily Request", ".github/workflows/other.yml", "123456"),
        ("AI Daily Request", "", "123456"),
        ("AI Daily Request", ".github/workflows/request.yml", ""),
        ("AI Daily Request", ".github/workflows/request.yml", "0"),
        ("AI Daily Request", ".github/workflows/request.yml", "12x"),
    ],
)
def test_production_authority_rejects_wrong_or_malformed_workflow_identity(
    tmp_path: Path, name: str, path: str, workflow_id: str
) -> None:
    """Would catch a same-name or same-path workflow impersonating a trusted request."""
    output = tmp_path / "github-output.txt"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_production_authority.py")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_SHA": AUTHORIZED_SHA,
            "DEFAULT_BRANCH": "main",
            "GITHUB_REPOSITORY": "owner/repository",
            "TRIGGER_EVENT": "repository_dispatch",
            "TRIGGER_HEAD_BRANCH": "main",
            "TRIGGER_HEAD_SHA": AUTHORIZED_SHA,
            "TRIGGER_HEAD_REPOSITORY": "owner/repository",
            "TRIGGER_CONCLUSION": "success",
            "EXPECTED_TRIGGER_WORKFLOW_NAME": "AI Daily Request",
            "EXPECTED_TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
            "TRIGGER_WORKFLOW_NAME": name,
            "TRIGGER_WORKFLOW_PATH": path,
            "TRIGGER_WORKFLOW_ID": workflow_id,
            "GITHUB_OUTPUT": str(output),
        },
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text(encoding="utf-8") == "trusted=false\nauthorized_sha=\n"


@pytest.mark.parametrize("repository", ["", "owner", "owner/repo/extra", "owner\n/repository"])
def test_production_authority_rejects_equal_but_malformed_repository_metadata(
    tmp_path: Path, repository: str
) -> None:
    """Would catch missing or malformed repository fields authorizing merely because they match."""
    output = tmp_path / "github-output.txt"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_production_authority.py")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "workflow_run",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REF_TYPE": "branch",
            "GITHUB_SHA": AUTHORIZED_SHA,
            "DEFAULT_BRANCH": "main",
            "GITHUB_REPOSITORY": repository,
            "TRIGGER_EVENT": "repository_dispatch",
            "TRIGGER_HEAD_BRANCH": "main",
            "TRIGGER_HEAD_SHA": AUTHORIZED_SHA,
            "TRIGGER_HEAD_REPOSITORY": repository,
            "TRIGGER_CONCLUSION": "success",
            "EXPECTED_TRIGGER_WORKFLOW_NAME": "AI Daily Request",
            "EXPECTED_TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
            "TRIGGER_WORKFLOW_NAME": "AI Daily Request",
            "TRIGGER_WORKFLOW_PATH": ".github/workflows/request.yml",
            "TRIGGER_WORKFLOW_ID": "123456",
            "GITHUB_OUTPUT": str(output),
        },
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text(encoding="utf-8") == "trusted=false\nauthorized_sha=\n"


def test_delivery_paths_require_trusted_ref_but_preview_remains_branch_safe() -> None:
    """Would catch a branch-authored workflow receiving the production token or secrets."""
    workflow = load_workflow("daily.yml")
    authorize = workflow["jobs"]["authorize"]
    daily = workflow["jobs"]["daily"]
    cost_preview = workflow["jobs"]["cost_preview"]

    assert authorize["permissions"] == {"contents": "read"}
    assert "environment" not in authorize
    assert "secrets." not in str(authorize)
    gate = next(step for step in authorize["steps"] if step.get("id") == "authority")
    assert gate["run"] == "python scripts/check_production_authority.py"
    assert gate["env"]["TRIGGER_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert gate["env"]["TRIGGER_WORKFLOW_NAME"] == "${{ github.event.workflow_run.name }}"
    assert gate["env"]["TRIGGER_WORKFLOW_PATH"] == "${{ github.event.workflow_run.path }}"
    assert gate["env"]["TRIGGER_WORKFLOW_ID"] == "${{ github.event.workflow_run.workflow_id }}"
    assert gate["env"]["EXPECTED_TRIGGER_WORKFLOW_NAME"] == "AI Daily Request"
    assert gate["env"]["EXPECTED_TRIGGER_WORKFLOW_PATH"] == ".github/workflows/request.yml"

    protected_names = {
        "Reserve delivery intent",
        "Commit reserved intent",
        "Release unattempted intent",
        "Commit unattempted cleanup",
        "Commit accepted state",
    }
    for step in daily["steps"]:
        if step.get("name") in protected_names:
            assert "needs.authorize.outputs.trusted == 'true'" in step["if"]

    assert daily["needs"] == "authorize"
    assert daily["environment"] == "ai-daily-production"
    assert daily["permissions"] == {"contents": "read"}
    assert "needs.authorize.outputs.trusted == 'true'" in daily["if"]
    assert cost_preview["needs"] == "authorize"
    assert cost_preview["environment"] == "ai-daily-production"
    assert cost_preview["permissions"] == {"contents": "read"}
    assert "needs.authorize.outputs.trusted == 'true'" in cost_preview["if"]


def test_manual_controls_use_default_branch_repository_dispatch_only() -> None:
    """Would catch an operator selecting a feature-authored workflow with elevated permissions."""
    workflows = {
        path.name: load_workflow(path.name)
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
    }

    assert all("workflow_dispatch" not in workflow[True] for workflow in workflows.values())
    assert workflows["request.yml"][True] == {
        "repository_dispatch": {"types": ["ai-daily-request"]}
    }
    assert workflows["resolve-delivery-request.yml"][True] == {
        "repository_dispatch": {"types": ["ai-daily-resolve"]}
    }
    assert workflows["validate-sources.yml"][True] == {
        "repository_dispatch": {"types": ["ai-daily-validate-sources"]}
    }


def test_manual_request_workflow_is_trusted_read_only_and_checks_out_target_as_data() -> None:
    """Would catch feature code defining the running workflow or receiving secret/write authority."""
    workflow = load_workflow("request.yml")
    job = workflow["jobs"]["safe_preview"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    serialized = yaml.safe_dump(workflow, sort_keys=True)

    assert workflow["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    assert "secrets." not in serialized
    assert "ai-daily preview --ai-mode off" in commands
    assert "--send" not in commands
    assert "reserve-delivery" not in commands
    assert "git push" not in commands
    assert "mode=${{ github.event.client_payload.mode }}" in workflow["run-name"]
    assert "ref=${{ github.event.client_payload.target_ref }}" in workflow["run-name"]
    validation = next(step for step in job["steps"] if step.get("id") == "request")
    assert validation["run"] == "python scripts/parse_repository_dispatch.py daily"
    checkouts = [
        step for step in job["steps"] if "actions/checkout@" in step.get("uses", "")
    ]
    assert checkouts[0]["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    assert checkouts[1]["with"] == {
        "ref": "${{ steps.request.outputs.target_ref }}",
        "path": "target",
        "persist-credentials": False,
    }
    assert job["steps"].index(validation) < job["steps"].index(checkouts[1])
    preview = next(step for step in job["steps"] if step.get("name") == "Run deterministic safe preview")
    assert preview["working-directory"] == "target"
    artifact = next(step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", ""))
    assert artifact["with"]["name"] == "ai-daily-safe-preview"
    assert artifact["with"]["path"] == "target/preview/"


def test_trusted_cost_preview_and_delivery_have_disjoint_authority() -> None:
    """Would catch Microsoft secrets or a write token entering a no-send cost preview."""
    workflow = load_workflow("daily.yml")
    preview = workflow["jobs"]["cost_preview"]
    delivery = workflow["jobs"]["daily"]
    preview_run = next(
        step for step in preview["steps"] if step.get("name") == "Run trusted cost preview"
    )
    delivery_run = next(
        step for step in delivery["steps"] if step.get("name") == "Run daily digest"
    )

    assert set(preview_run["env"]) == {"DEEPSEEK_API_KEY", "GITHUB_TOKEN"}
    assert all(name not in str(preview) for name in ("MS_CLIENT_ID", "MS_TOKEN_KEY", "OUTLOOK_SENDER", "MAIL_TO"))
    assert "ai-daily preview" in preview_run["run"]
    assert "--send" not in preview_run["run"]
    assert set(delivery_run["env"]) == {
        "DEEPSEEK_API_KEY",
        "MS_CLIENT_ID",
        "MS_TOKEN_KEY",
        "OUTLOOK_SENDER",
        "MAIL_TO",
        "GITHUB_TOKEN",
    }
    assert "ai-daily run --send" in delivery_run["run"]


def test_builtin_tokens_are_read_only_and_state_token_is_confined_to_write_jobs() -> None:
    """Would catch feature-authored workflow code escalating the built-in token or seeing state auth."""
    workflow_paths = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    serialized_by_path = {
        path.name: yaml.safe_dump(load_workflow(path.name), sort_keys=True)
        for path in workflow_paths
    }
    for path in workflow_paths:
        workflow = load_workflow(path.name)
        assert workflow.get("permissions", {}) in ({}, {"contents": "read"})
        for job in workflow.get("jobs", {}).values():
            assert job.get("permissions", {}) in ({}, {"contents": "read"})
        assert "contents: write" not in serialized_by_path[path.name]

    daily = load_workflow("daily.yml")["jobs"]
    recovery = load_workflow("resolve-delivery.yml")["jobs"]
    state_token = "${{ secrets.AI_DAILY_STATE_TOKEN }}"
    assert daily["daily"]["environment"] == "ai-daily-production"
    assert recovery["resolve"]["environment"] == "ai-daily-production"

    allowed_steps = {
        ("daily.yml", "daily", "Push reserved intent"),
        ("daily.yml", "daily", "Push unattempted cleanup"),
        ("daily.yml", "daily", "Push accepted state"),
        ("resolve-delivery.yml", "resolve", "Push resolved state"),
    }
    for path in workflow_paths:
        for job_name, job in load_workflow(path.name).get("jobs", {}).items():
            for step in job["steps"]:
                identity = (path.name, job_name, step.get("name", ""))
                if identity in allowed_steps:
                    assert step["env"] == {"AI_DAILY_STATE_TOKEN": state_token}
                    assert step["run"] == "python scripts/push_production_state.py"
                else:
                    assert "AI_DAILY_STATE_TOKEN" not in str(step)
                    assert state_token not in str(step)


def test_all_checkouts_use_read_token_without_persisted_credentials() -> None:
    """Would catch a token-bearing git config surviving into untrusted code or later steps."""
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for job in load_workflow(path.name).get("jobs", {}).values():
            for step in job["steps"]:
                if "actions/checkout@" not in step.get("uses", ""):
                    continue
                assert step.get("with", {}).get("persist-credentials") is False
                assert "token" not in step.get("with", {})


def test_production_checkouts_are_pinned_and_state_credentials_are_ephemeral() -> None:
    """Would catch trusted code advancing to an unvalidated mutable default-branch tip."""
    daily = load_workflow("daily.yml")["jobs"]
    recovery = load_workflow("resolve-delivery.yml")["jobs"]

    daily_authorize_checkout = next(
        step for step in daily["authorize"]["steps"] if "actions/checkout@" in step.get("uses", "")
    )
    assert daily_authorize_checkout["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    assert daily["authorize"]["outputs"]["authorized_sha"] == (
        "${{ steps.authority.outputs.authorized_sha }}"
    )

    for job in (daily["cost_preview"], daily["daily"], recovery["resolve"]):
        checkout = next(
            step for step in job["steps"] if "actions/checkout@" in step.get("uses", "")
        )
        assert checkout["with"]["ref"] == "${{ needs.authorize.outputs.authorized_sha }}"

    preview_checkout = next(
        step
        for step in daily["cost_preview"]["steps"]
        if "actions/checkout@" in step.get("uses", "")
    )
    assert preview_checkout["with"]["persist-credentials"] is False
    assert "token" not in preview_checkout["with"]

    for workflow_name in ("daily.yml", "resolve-delivery.yml"):
        workflow = load_workflow(workflow_name)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "actions/upload-artifact@" in step.get("uses", "") or step.get("name") in {
                    "Run trusted cost preview",
                    "Run daily digest",
                    "Run deterministic safe preview",
                }:
                    assert "AI_DAILY_STATE_TOKEN" not in str(step)

    recovery_authorize_checkout = next(
        step
        for step in recovery["authorize"]["steps"]
        if "actions/checkout@" in step.get("uses", "")
    )
    assert recovery_authorize_checkout["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }


@pytest.mark.parametrize(
    ("request_type", "title", "expected"),
    [
        (
            "daily",
            (
                "ai-daily-request|mode=economy|send=false|force=true|"
                "ref=refs/heads/main"
            ),
            {"mode": "economy", "send": "false", "force": "true"},
        ),
        (
            "resolve",
            (
                "ai-daily-resolve|date=2026-08-24|resolution=sent|"
                "message_id=mailbox-confirmed-2026-08-24|confirm=true|"
                "ref=refs/heads/main"
            ),
            {
                "date": "2026-08-24",
                "resolution": "sent",
                "message_id": "mailbox-confirmed-2026-08-24",
                "confirm": "true",
            },
        ),
    ],
)
def test_trusted_request_parser_accepts_only_bounded_allowlisted_fields(
    tmp_path: Path, request_type: str, title: str, expected: dict[str, str]
) -> None:
    """Would catch a crafted run title injecting commands or unsupported production inputs."""
    output = tmp_path / "github-output.txt"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "parse_trusted_request.py"), request_type],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REQUEST_TITLE": title,
            "DEFAULT_BRANCH": "main",
            "GITHUB_OUTPUT": str(output),
        },
        timeout=5,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines()) == expected


@pytest.mark.parametrize(
    ("request_type", "title"),
    [
        ("daily", "ai-daily-request|mode=full|send=true|force=false\ncommand=bad"),
        ("daily", "ai-daily-request|mode=invalid|send=true|force=false"),
        (
            "daily",
            (
                "ai-daily-request|mode=full|send=true|force=false|"
                "ref=refs/tags/main"
            ),
        ),
        (
            "resolve",
            "ai-daily-resolve|date=2026-8-24|resolution=retry|message_id=|confirm=true",
        ),
        (
            "resolve",
            "ai-daily-resolve|date=2026-08-24|resolution=retry|message_id=unsafe@id|confirm=true",
        ),
    ],
)
def test_trusted_request_parser_rejects_malformed_or_injected_titles(
    tmp_path: Path, request_type: str, title: str
) -> None:
    """Would catch malformed workflow-run metadata falling through to production commands."""
    output = tmp_path / "github-output.txt"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "parse_trusted_request.py"), request_type],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REQUEST_TITLE": title,
            "DEFAULT_BRANCH": "main",
            "GITHUB_OUTPUT": str(output),
        },
        timeout=5,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert not output.exists() or output.read_text("utf-8") == ""


def test_manual_resolution_workflow_is_serialized_state_only_and_default_branch_gated() -> None:
    """Would catch operator recovery racing delivery or mutating non-state paths."""
    request_workflow = load_workflow("resolve-delivery-request.yml")
    request_trigger = request_workflow[True]
    assert request_trigger == {
        "repository_dispatch": {"types": ["ai-daily-resolve"]}
    }
    assert request_workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in yaml.safe_dump(request_workflow, sort_keys=True)
    assert all(
        job.get("permissions") == {"contents": "read"} and "environment" not in job
        for job in request_workflow["jobs"].values()
    )
    assert "ref=${{ github.event.client_payload.target_ref }}" in request_workflow["run-name"]
    request_steps = request_workflow["jobs"]["record_request"]["steps"]
    request_validation = next(step for step in request_steps if step.get("id") == "request")
    assert request_validation["run"] == "python scripts/parse_repository_dispatch.py resolve"

    workflow = load_workflow("resolve-delivery.yml")
    trigger = workflow[True]
    assert trigger == {
        "workflow_run": {
            "workflows": ["Resolve AI Daily Delivery Request"],
            "types": ["completed"],
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "ai-daily-delivery",
        "cancel-in-progress": False,
    }

    authorize = workflow["jobs"]["authorize"]
    assert authorize["permissions"] == {"contents": "read"}
    assert "environment" not in authorize
    assert "secrets." not in str(authorize)
    recovery_gate = next(step for step in authorize["steps"] if step.get("id") == "authority")
    assert recovery_gate["env"]["EXPECTED_TRIGGER_WORKFLOW_NAME"] == (
        "Resolve AI Daily Delivery Request"
    )
    assert recovery_gate["env"]["EXPECTED_TRIGGER_WORKFLOW_PATH"] == (
        ".github/workflows/resolve-delivery-request.yml"
    )
    assert recovery_gate["env"]["TRIGGER_WORKFLOW_NAME"] == (
        "${{ github.event.workflow_run.name }}"
    )
    assert recovery_gate["env"]["TRIGGER_WORKFLOW_PATH"] == (
        "${{ github.event.workflow_run.path }}"
    )
    assert workflow["jobs"]["resolve"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["resolve"]["environment"] == "ai-daily-production"
    assert workflow["jobs"]["resolve"]["needs"] == "authorize"
    assert "needs.authorize.outputs.trusted == 'true'" in workflow["jobs"]["resolve"]["if"]

    steps = workflow["jobs"]["resolve"]["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
    ]
    resolution = next(step for step in steps if step.get("name") == "Resolve delivery state")
    state_commit = next(step for step in steps if step.get("name") == "Commit resolved state")
    assert steps.index(resolution) < steps.index(state_commit)
    assert "needs.authorize.outputs.trusted == 'true'" in resolution["if"]
    assert "needs.authorize.outputs.confirm == 'true'" in resolution["if"]
    assert "--confirm-owner-terminal" in resolution["run"]
    assert "needs.authorize.outputs.trusted == 'true'" in state_commit["if"]
    assert "git add -- data/state.json" in state_commit["run"]
    assert "git add -A" not in state_commit["run"]
    assert "git push --force" not in state_commit["run"]
    assert "git pull" not in state_commit["run"]
    assert "git push" not in state_commit["run"]
    state_push = next(step for step in steps if step.get("name") == "Push resolved state")
    assert steps.index(state_commit) < steps.index(state_push)


def test_remote_default_is_verified_before_first_production_mutation() -> None:
    """Would catch a protected branch advancing after authorization but before state mutation."""
    daily_steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    resolve_steps = load_workflow("resolve-delivery.yml")["jobs"]["resolve"]["steps"]

    daily_verify = next(
        index for index, step in enumerate(daily_steps) if step.get("name") == "Verify immutable default tip"
    )
    daily_reserve = next(
        index for index, step in enumerate(daily_steps) if step.get("name") == "Reserve delivery intent"
    )
    recovery_verify = next(
        index for index, step in enumerate(resolve_steps) if step.get("name") == "Verify immutable default tip"
    )
    recovery_mutate = next(
        index for index, step in enumerate(resolve_steps) if step.get("name") == "Resolve delivery state"
    )

    assert daily_verify < daily_reserve
    assert recovery_verify < recovery_mutate
    for steps, index in ((daily_steps, daily_verify), (resolve_steps, recovery_verify)):
        assert steps[index]["run"] == "python scripts/check_remote_default.py"
        assert steps[index]["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}


def test_manual_resolution_dispatch_accepts_only_retry_and_sent() -> None:
    """Would catch an unknown or missing resolution falling through to a state mutation."""
    steps = load_workflow("resolve-delivery.yml")["jobs"]["resolve"]["steps"]
    command = next(step["run"] for step in steps if step.get("name") == "Resolve delivery state")
    branches = dict(
        re.findall(
            r"^\s*([^()\s]+)\)\s*$\n(.*?)^\s*;;\s*$",
            command,
            flags=re.MULTILINE | re.DOTALL,
        )
    )

    assert 'case "${RESOLUTION:-}" in' in command
    assert list(branches) == ["retry", "sent", "*"]
    assert "resolve-delivery retry" in branches["retry"]
    assert "resolve-delivery sent" not in branches["retry"]
    assert "resolve-delivery sent" in branches["sent"]
    assert "resolve-delivery retry" not in branches["sent"]
    assert "ai-daily" not in branches["*"]
    assert "exit 1" in branches["*"]


def test_daily_delivery_concurrency_is_shared_across_all_repository_refs() -> None:
    """Would catch branch-scoped locks allowing two refs to reserve/send the same date."""
    concurrency = load_workflow("daily.yml")["concurrency"]

    assert concurrency == {
        "group": "ai-daily-delivery",
        "cancel-in-progress": False,
    }


def test_persistent_delivery_lock_artifact_is_excluded_from_git_state_commits() -> None:
    """Would catch the OS lock file being staged by the workflow's restricted data commit."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "data/state.json.lock" in ignored


def test_daily_persists_durable_state_before_nonessential_artifact_upload() -> None:
    """Would catch an artifact outage preventing the sent marker from being committed after send."""
    workflow = load_workflow("daily.yml")
    steps = workflow["jobs"]["daily"]["steps"]
    run_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Run daily digest"
    )
    commit_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Commit accepted state"
    )
    push_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Push accepted state"
    )
    artifact_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )

    assert run_index < commit_index < push_index < artifact_index
    assert commit_index > run_index
    assert steps[artifact_index]["if"] == "always()"


def test_daily_commits_reservation_before_any_graph_capable_step() -> None:
    """Would catch send running after only a local, unpushed reservation."""
    workflow = load_workflow("daily.yml")
    steps = workflow["jobs"]["daily"]["steps"]
    reserve_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Reserve delivery intent"
    )
    intent_commit_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Commit reserved intent"
    )
    intent_push_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Push reserved intent"
    )
    digest_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Run daily digest"
    )
    reserve = steps[reserve_index]
    digest = steps[digest_index]

    assert reserve_index < intent_commit_index < intent_push_index < digest_index
    assert "github.run_id" in workflow["jobs"]["daily"]["env"]["AI_DAILY_ATTEMPT_ID"]
    assert "github.run_attempt" in workflow["jobs"]["daily"]["env"]["AI_DAILY_ATTEMPT_ID"]
    assert "--attempt-id" in reserve["run"]
    assert "--attempt-id" in digest["run"]
    assert "steps.reserve.outputs.reservation_status" in digest["if"]


def test_daily_cleanup_is_only_for_explicit_not_attempted_output() -> None:
    """Would catch ambiguous, accepted, or missing runner output clearing remote intent."""
    steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    cleanup = next(step for step in steps if step.get("name") == "Release unattempted intent")
    cleanup_commit = next(
        step for step in steps if step.get("name") == "Commit unattempted cleanup"
    )
    cleanup_push = next(
        step for step in steps if step.get("name") == "Push unattempted cleanup"
    )

    assert "steps.digest.outputs.delivery_outcome == 'not_attempted'" in cleanup["if"]
    assert "always()" in cleanup["if"]
    assert "steps.cleanup.outcome == 'success'" in cleanup_commit["if"]
    assert "steps.cleanup_commit.outcome == 'success'" in cleanup_push["if"]
    assert "release-delivery" in cleanup["run"]


def test_daily_final_commit_requires_successful_accepted_delivery() -> None:
    """Would catch Graph/state failure or missing output publishing a false completed marker."""
    steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    final_commit = next(step for step in steps if step.get("name") == "Commit accepted state")

    assert "steps.digest.outcome == 'success'" in final_commit["if"]
    assert "steps.digest.outputs.delivery_outcome == 'accepted'" in final_commit["if"]
    assert "git push" not in final_commit["run"]
    final_push = next(step for step in steps if step.get("name") == "Push accepted state")
    assert "steps.digest.outputs.delivery_outcome == 'accepted'" in final_push["if"]


def test_manual_source_validation_is_read_only_locked_and_pinned() -> None:
    """Would catch diagnostics gaining mail/state authority or mutable dependencies/actions."""
    workflow = load_workflow("validate-sources.yml")
    trigger = workflow[True]
    assert trigger == {
        "repository_dispatch": {"types": ["ai-daily-validate-sources"]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["validate"]["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
    ]
    commands = "\n".join(step.get("run", "") for step in steps)
    request = next(step for step in steps if step.get("id") == "request")
    assert request["run"] == "python scripts/parse_repository_dispatch.py sources"
    assert "pip install --requirement requirements-prod.lock" in commands
    assert "pip install --no-deps --no-build-isolation -e ." in commands
    assert 'validate-sources --minimum-success "${{ steps.request.outputs.minimum_success }}"' in commands
    assert "run --send" not in commands
    assert "git push" not in commands
    assert "secrets." not in commands


def test_operator_guide_contains_private_outlook_and_safety_invariants() -> None:
    """Would catch the handoff omitting secret handling, cost review, or delivery limitations."""
    guide = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "私有仓库",
        "MS_TOKEN_KEY",
        "AI_DAILY_STATE_TOKEN",
        "GITHUB_TOKEN",
        "仅表示 Microsoft Graph 已接受/排队",
        "首次运行成本审阅",
        "撤销",
        "轮换",
        "补偿",
        "send=false",
        "mode=full`、`send=false",
        "允许公共客户端流",
        "Fernet.generate_key",
        "--replace",
        "AI_DAILY_SCHEDULE_ENABLED",
        "requirements-prod.lock",
        "--no-deps --no-build-isolation -e .",
        "可编辑仓库检出",
        "Read repository contents and packages",
        "AI_DAILY_STATE_TOKEN",
        "必须",
    ):
        assert phrase in guide


def test_operator_guide_defines_personal_same_repository_writer_trust_boundary() -> None:
    """Would catch preview isolation being overstated as a sandbox for repository writers."""
    guide = (ROOT / "README.md").read_text(encoding="utf-8")

    for phrase in (
        "个人账户/单一受信维护者",
        "same-repository write access 对内建 `GITHUB_TOKEN` 是受信任的",
        "绝不要把同一仓库 Write 权限授予不受信任的人",
        "不受信任的贡献必须来自 fork",
        "Send write tokens to workflows from pull requests",
        "Send secrets to workflows from pull requests",
        "Require approval for all outside collaborators",
        "Require approval for first-time contributors",
        "Read repository contents and packages",
        "只是纵深防御，不是权限上限",
        "默认分支必须受保护",
        "`ai-daily-production` Environment 必须只允许该默认分支",
        "生产 Secrets 和 `AI_DAILY_STATE_TOKEN`",
        "`target_ref`",
        "不是对已经获得同仓库 Write 权限者的仓库级沙箱",
    ):
        assert phrase in guide

    assert "它们不能获得生产 Secret、写权限" not in guide


def test_operator_guide_requires_serialized_terminal_owner_recovery() -> None:
    """Would catch docs telling an operator to clear intent while its owner can still send."""
    guide = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Resolve AI Daily Delivery" in guide
    assert "ai-daily-delivery" in guide
    assert "运行仍为 queued 或 in_progress 时绝不能执行恢复" in guide
    assert "确认拥有该 intent 的运行已经进入终态" in guide
    assert (
        "ai-daily resolve-delivery retry --date 2026-08-24 "
        "--confirm-owner-terminal"
    ) in guide
    assert (
        "ai-daily resolve-delivery sent --date 2026-08-24 "
        "--message-id mailbox-confirmed-2026-08-24 --confirm-owner-terminal"
    ) in guide


def test_package_metadata_declares_editable_repository_runtime_contract() -> None:
    """Would catch a published-wheel expectation despite repository-only runtime assets."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "editable repository checkout" in metadata["project"]["description"]
