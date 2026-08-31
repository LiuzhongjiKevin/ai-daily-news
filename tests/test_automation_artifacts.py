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
    steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]


def test_workflows_install_exact_locked_dependency_sets_before_editable_package() -> None:
    """Would catch Actions resolving broad package ranges instead of the reviewed lock files."""
    ci_steps = load_workflow("ci.yml")["jobs"]["test"]["steps"]
    daily_steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    ci_commands = "\n".join(step.get("run", "") for step in ci_steps)
    daily_commands = "\n".join(step.get("run", "") for step in daily_steps)

    assert "pip install --requirement requirements-dev.lock" in ci_commands
    assert 'pip install --no-deps --no-build-isolation -e ".[dev]"' in ci_commands
    assert "pip install --requirement requirements-prod.lock" in daily_commands
    assert 'pip install --no-deps --no-build-isolation -e .' in daily_commands
    assert ".[dev]" not in daily_commands

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
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = workflow["jobs"]["daily"]["steps"]
    command = next(step["run"] for step in steps if step.get("name") == "Run daily digest")
    assert 'github.event_name' in command and 'ai-daily run --send' in command
    commits = [
        step["run"]
        for step in steps
        if step.get("name")
        in {"Commit reserved intent", "Commit unattempted cleanup", "Commit accepted state"}
    ]
    assert len(commits) == 3
    assert "git add -- data/ digests/ data/microsoft-token.enc" in commits[-1]
    assert all("git add -A" not in commit for commit in commits)
    assert all("git pull --rebase --autostash" in commit for commit in commits)


def test_scheduled_delivery_requires_explicit_repository_variable_gate() -> None:
    """Would catch a default-branch push activating scheduled sends before cost review."""
    workflow = load_workflow("daily.yml")
    gate = workflow["jobs"]["daily"]["if"]
    assert "github.event_name != 'schedule'" in gate
    assert "vars.AI_DAILY_SCHEDULE_ENABLED == 'true'" in gate


@pytest.mark.parametrize(
    ("case", "ref", "ref_type", "default_branch", "trusted"),
    [
        ("scheduled default", "refs/heads/main", "branch", "main", True),
        ("manual default", "refs/heads/main", "branch", "main", True),
        ("ordinary feature", "refs/heads/feature/safe-preview", "branch", "main", False),
        ("case collision", "refs/heads/Main", "branch", "main", False),
        ("same-named tag", "refs/tags/main", "tag", "main", False),
    ],
)
def test_delivery_ref_gate_uses_exact_full_branch_ref(
    tmp_path: Path,
    case: str,
    ref: str,
    ref_type: str,
    default_branch: str,
    trusted: bool,
) -> None:
    """Would catch GitHub's case-insensitive expression equality authorizing another ref."""
    output = tmp_path / "github-output.txt"
    environment = {
        **os.environ,
        "GITHUB_REF": ref,
        "GITHUB_REF_TYPE": ref_type,
        "DEFAULT_BRANCH": default_branch,
        "GITHUB_OUTPUT": str(output),
    }

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_delivery_ref.py")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert completed.returncode == 0, case
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_text(encoding="utf-8") == f"trusted={str(trusted).lower()}\n"


def test_delivery_paths_require_trusted_ref_but_preview_remains_branch_safe() -> None:
    """Would catch a feature branch reserving, sending, or pushing centralized state."""
    workflow = load_workflow("daily.yml")
    job = workflow["jobs"]["daily"]
    steps = job["steps"]
    gate_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "delivery_ref"
    )
    gate = steps[gate_index]

    assert "github.ref_name == github.event.repository.default_branch" not in job["if"]
    assert gate["env"] == {"DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}"}
    assert gate["run"] == "python scripts/check_delivery_ref.py"

    protected_names = {
        "Reserve delivery intent",
        "Commit reserved intent",
        "Release unattempted intent",
        "Commit unattempted cleanup",
        "Commit accepted state",
    }
    for step in steps:
        if step.get("name") in protected_names:
            assert "steps.delivery_ref.outputs.trusted == 'true'" in step["if"]

    digest_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Run daily digest"
    )
    digest = steps[digest_index]
    assert gate_index < digest_index
    assert "github.event_name == 'workflow_dispatch' && inputs.send != true" in digest["if"]
    assert "steps.delivery_ref.outputs.trusted == 'true'" in digest["if"]
    assert "ai-daily preview" in digest["run"]


def test_manual_resolution_workflow_is_serialized_state_only_and_default_branch_gated() -> None:
    """Would catch operator recovery racing delivery or mutating non-state paths."""
    workflow = load_workflow("resolve-delivery.yml")
    trigger = workflow[True]["workflow_dispatch"]["inputs"]
    assert set(trigger) == {
        "date",
        "resolution",
        "message_id",
        "confirm_owner_terminal",
    }
    assert trigger["resolution"]["options"] == ["retry", "sent"]
    assert trigger["confirm_owner_terminal"]["type"] == "boolean"
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"] == {
        "group": "ai-daily-delivery",
        "cancel-in-progress": False,
    }

    steps = workflow["jobs"]["resolve"]["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
    ]
    ref_gate = next(step for step in steps if step.get("id") == "delivery_ref")
    context_gate = next(
        step for step in steps if step.get("name") == "Require trusted recovery context"
    )
    resolution = next(step for step in steps if step.get("name") == "Resolve delivery state")
    state_commit = next(step for step in steps if step.get("name") == "Commit resolved state")
    assert steps.index(ref_gate) < steps.index(context_gate) < steps.index(resolution)
    assert steps.index(resolution) < steps.index(state_commit)
    assert ref_gate["run"] == "python scripts/check_delivery_ref.py"
    assert "TRUSTED_REF" in context_gate["run"]
    assert "OWNER_TERMINAL_CONFIRMED" in context_gate["run"]
    assert "steps.delivery_ref.outputs.trusted == 'true'" in resolution["if"]
    assert "inputs.confirm_owner_terminal == true" in resolution["if"]
    assert "--confirm-owner-terminal" in resolution["run"]
    assert "steps.delivery_ref.outputs.trusted == 'true'" in state_commit["if"]
    assert "git add -- data/state.json" in state_commit["run"]
    assert "git add -A" not in state_commit["run"]
    assert "git push --force" not in state_commit["run"]
    assert "git push origin HEAD:" in state_commit["run"]


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
    artifact_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )

    assert run_index < commit_index < artifact_index
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
    digest_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Run daily digest"
    )
    reserve = steps[reserve_index]
    digest = steps[digest_index]

    assert reserve_index < intent_commit_index < digest_index
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

    assert "steps.digest.outputs.delivery_outcome == 'not_attempted'" in cleanup["if"]
    assert "always()" in cleanup["if"]
    assert "steps.cleanup.outcome == 'success'" in cleanup_commit["if"]
    assert "release-delivery" in cleanup["run"]


def test_daily_final_commit_requires_successful_accepted_delivery() -> None:
    """Would catch Graph/state failure or missing output publishing a false completed marker."""
    steps = load_workflow("daily.yml")["jobs"]["daily"]["steps"]
    final_commit = next(step for step in steps if step.get("name") == "Commit accepted state")

    assert "steps.digest.outcome == 'success'" in final_commit["if"]
    assert "steps.digest.outputs.delivery_outcome == 'accepted'" in final_commit["if"]
    assert "git push origin" in final_commit["run"]


def test_manual_source_validation_is_read_only_locked_and_pinned() -> None:
    """Would catch diagnostics gaining mail/state authority or mutable dependencies/actions."""
    workflow = load_workflow("validate-sources.yml")
    trigger = workflow[True]
    assert set(trigger) == {"workflow_dispatch"}
    assert trigger["workflow_dispatch"]["inputs"]["minimum_success"]["default"] == 80
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["validate"]["steps"]
    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38",
    ]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "pip install --requirement requirements-prod.lock" in commands
    assert "pip install --no-deps --no-build-isolation -e ." in commands
    assert "validate-sources --minimum-success" in commands
    assert "run --send" not in commands
    assert "git push" not in commands
    assert "secrets." not in commands


def test_operator_guide_contains_private_outlook_and_safety_invariants() -> None:
    """Would catch the handoff omitting secret handling, cost review, or delivery limitations."""
    guide = (ROOT / "README.md").read_text(encoding="utf-8")
    for phrase in (
        "私有仓库",
        "MS_TOKEN_KEY",
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
    ):
        assert phrase in guide


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
