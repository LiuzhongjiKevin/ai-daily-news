from __future__ import annotations

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
    commit = next(step["run"] for step in steps if step.get("name") == "Commit durable state")
    assert "git add -- data/ digests/ data/microsoft-token.enc" in commit
    assert "git add -A" not in commit
    assert "git pull --rebase --autostash" in commit


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
    ):
        assert phrase in guide
