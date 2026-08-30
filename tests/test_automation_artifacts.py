from __future__ import annotations

import tomllib
from pathlib import Path

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


def test_delivery_dispatch_is_default_branch_only_but_preview_remains_branch_safe() -> None:
    """Would catch a feature-branch manual send using state isolated from scheduled delivery."""
    workflow = load_workflow("daily.yml")
    gate = workflow["jobs"]["daily"]["if"]

    assert "github.ref_name == github.event.repository.default_branch" in gate
    assert "github.event_name == 'workflow_dispatch' && inputs.send != true" in gate


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


def test_package_metadata_declares_editable_repository_runtime_contract() -> None:
    """Would catch a published-wheel expectation despite repository-only runtime assets."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "editable repository checkout" in metadata["project"]["description"]
