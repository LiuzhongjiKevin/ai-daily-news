import subprocess
from datetime import UTC, datetime

import httpx
import pytest
from test_world_delivery import rows
from test_world_translation_budget import install, ledger

from ai_daily import world_finance as world
from ai_daily.state import StateStore
from ai_daily.world_alibaba import AlibabaTranslator

NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)

def test_workflow_translation_is_explicitly_gated_and_preview_has_no_keys():
    from pathlib import Path

    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                              ".github/workflows/world-finance.yml").read_text("utf-8"))
    preview = workflow["jobs"]["preview"]
    for step in preview["steps"]:
        assert not any("ACCESS_KEY" in key for key in step.get("env", {}))
    send = workflow["jobs"]["send"]
    assert send["environment"] == "world-finance-production"
    checkout = send["steps"][0]["with"]
    assert checkout["ref"] == "${{ needs.preview.outputs.source_sha }}"
    assert checkout["fetch-depth"] == 0
    environment = next(s["env"] for s in send["steps"] if "env" in s)
    assert environment["WORLD_ALIBABA_TRANSLATION_ENABLED"] == (
        "${{ vars.WORLD_ALIBABA_TRANSLATION_ENABLED }}")
    assert environment["WORLD_TRANSLATION_LEDGER_ANCHOR"] == (
        "${{ vars.WORLD_TRANSLATION_LEDGER_ANCHOR }}")
    assert "TENCENTCLOUD_SECRET_ID" not in environment


@pytest.mark.parametrize("payload,status,allowed", [
    ({"enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
      "allow_deletions": {"enabled": False}}, 200, True),
    ({"enforce_admins": {"enabled": False}, "allow_force_pushes": {"enabled": False},
      "allow_deletions": {"enabled": False}}, 200, False),
    ({"enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": True},
      "allow_deletions": {"enabled": False}}, 200, False),
    ({}, 404, False),
    ({}, 403, False),
])
def test_append_only_protection_must_be_verified(monkeypatch, payload, status, allowed):
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("WORLD_FINANCE_STATE_TOKEN", "fake-state")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)))
    from ai_daily.world_translation_budget import BudgetUnavailable
    if allowed:
        world.verify_append_only_protection(client=client)
    else:
        with pytest.raises(BudgetUnavailable):
            world.verify_append_only_protection(client=client)


def test_actual_git_history_rejects_revert_and_stale_checkout(tmp_path, monkeypatch):
    # Git object reads are real; only external metadata reads are replaced.
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    store = StateStore(tmp_path / "world-state")
    install(store, ledger())
    def commit():
        git("add", "world-state/state.json")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", "Fixture state")
        return git("rev-parse", "HEAD")
    anchor = commit()
    monkeypatch.setenv("WORLD_TRANSLATION_LEDGER_ANCHOR", anchor)
    monkeypatch.setattr(world, "_remote_tip", lambda root: git("rev-parse", "HEAD"))
    monkeypatch.setattr(world, "verify_append_only_protection", lambda: None)
    world.verify_budget_history(tmp_path)
    from ai_daily.world_translation_budget import BudgetUnavailable, make_manifest, reserve
    reserve(store, make_manifest(["Trade"]), NOW, "123", "1")
    commit()
    world.verify_budget_history(tmp_path)
    install(store, ledger())
    commit()  # ordinary revert commit: ledger total decreases, history must detect it
    with pytest.raises(BudgetUnavailable):
        world.verify_budget_history(tmp_path)
    monkeypatch.setattr(world, "_remote_tip", lambda root: "f" * 40)
    with pytest.raises(BudgetUnavailable):
        world.verify_budget_history(tmp_path)


def test_new_alibaba_keys_are_removed_from_shared_git_push_environment(monkeypatch):
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "push_production_state.py"
    spec = importlib.util.spec_from_file_location("world_test_push", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "fake-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "fake-secret")
    monkeypatch.setenv("AI_MEMORY_AUTH_TOKEN", "fake-memory-token")
    env = module._git_environment("fake-token")
    present = any(key in env for key in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "AI_MEMORY_AUTH_TOKEN"))
    assert not present



def enable(monkeypatch, tmp_path):
    monkeypatch.setenv("WORLD_ALIBABA_TRANSLATION_ENABLED", "true")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "fake-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "fake-secret")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(world, "verify_budget_history", lambda root: None)
    monkeypatch.setattr(world, "translation_now", lambda: NOW)
    store = StateStore(tmp_path / "world-state")
    install(store, ledger())
    return store


class Mailer:
    def __init__(self):
        self.sent = []
    def send(self, rendered):
        self.sent.append(rendered)
        return "accepted-world-alibaba"


def test_translation_only_after_pushed_budget_and_duplicate_skips(tmp_path, monkeypatch):
    store = enable(monkeypatch, tmp_path)
    events = []
    original = AlibabaTranslator.__init__
    def init(self, *args, **kwargs):
        def handler(request):
            assert events == ["checkpoint"]
            budget = store.load_run_state().translation_budget
            assert budget["months"]["2026-11"]["reserved"] == 11
            events.append("translate")
            return httpx.Response(200, json={"Code": 200, "Data": {"Translated": "贸易和平"}})
        original(self, *args, client=httpx.Client(transport=httpx.MockTransport(handler)),
                 clock=lambda: NOW)
    monkeypatch.setattr(AlibabaTranslator, "__init__", init)
    def checkpoint():
        events.append("checkpoint")
        return "a" * 40
    mailer = Mailer()
    assert world.deliver(tmp_path, rows(), NOW, [], mailer, checkpoint) == "accepted"
    assert "贸易和平" in mailer.sent[0].text
    assert events == ["checkpoint", "translate", "checkpoint"]
    assert world.deliver(tmp_path, rows(), NOW, [], mailer, checkpoint) == "already_sent"
    assert len(mailer.sent) == 1
    assert events == ["checkpoint", "translate", "checkpoint"]


@pytest.mark.parametrize("outcome", ["failed", "unknown", "unconfirmed"])
def test_failed_or_uncertain_push_never_translates(tmp_path, monkeypatch, outcome):
    store = enable(monkeypatch, tmp_path)
    monkeypatch.setattr(AlibabaTranslator, "__init__",
                        lambda *a, **k: pytest.fail("Must not construct transport"))
    def checkpoint():
        if outcome == "failed":
            raise RuntimeError("failed")
        if outcome == "unknown":
            raise subprocess.TimeoutExpired("git", 60)
    mailer = Mailer()
    if outcome == "unconfirmed":
        assert world.deliver(tmp_path, rows(), NOW, [], mailer, checkpoint) == "accepted"
    else:
        with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
            world.deliver(tmp_path, rows(), NOW, [], mailer, checkpoint)
        assert not mailer.sent
    assert store.load_run_state().translation_budget["months"]["2026-11"]["reserved"] == 11


@pytest.mark.parametrize("mode", ["unknown", "exhausted", "rollback", "history_unavailable"])
def test_unverified_budget_sends_original_without_provider(tmp_path, monkeypatch, mode):
    store = enable(monkeypatch, tmp_path)
    if mode == "unknown":
        install(store, None)
    if mode == "exhausted":
        install(store, ledger(baseline=300000))
    if mode in {"rollback", "history_unavailable"}:
        from ai_daily.world_translation_budget import BudgetUnavailable
        def fail(root):
            raise BudgetUnavailable("budget_history")
        monkeypatch.setattr(world, "verify_budget_history", fail)
    monkeypatch.setattr(AlibabaTranslator, "__init__",
                        lambda *a, **k: pytest.fail("Provider must not be initialized"))
    mailer = Mailer()
    assert world.deliver(tmp_path, rows(), NOW, [], mailer, lambda: "a" * 40) == "accepted"
    assert "Trade peace" in mailer.sent[0].text


def test_all_git_subprocesses_strip_translation_credentials(tmp_path, monkeypatch):
    for key in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "AI_MEMORY_AUTH_TOKEN"):
        monkeypatch.setenv(key, "must-not-reach-git")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("WORLD_FINANCE_ENVIRONMENT", "world-finance-production")
    monkeypatch.setenv("WORLD_FINANCE_STATE_TOKEN", "fake-state-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    commands = []
    diffs = iter(["", "world-state/state.json"])
    def run(command, **kwargs):
        assert kwargs["env"] is not None
        leaked = "must-not-reach-git" in kwargs["env"].values()
        assert not leaked
        commands.append(command)
        output = ""
        if command[1:4] == ["diff", "--cached", "--name-only"]:
            output = next(diffs)
        elif command[1] == "rev-parse":
            output = "a" * 40
        elif command[1] == "ls-remote":
            output = "a" * 40 + "\trefs/heads/world-finance-daily"
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(world.subprocess, "run", run)
    assert world.checkpoint_state(tmp_path) == "a" * 40
    assert not any("--force" in part for command in commands for part in command)


def test_two_competing_git_reservations_only_one_can_spend(tmp_path, monkeypatch):
    # Real Git non-fast-forward barrier, no network. Both runners start at the same state.
    real_run = subprocess.run
    def git(cwd, *args):
        return real_run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "checkout", "-b", "world-finance-daily")
    state = StateStore(source / "world-state")
    install(state, ledger())
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "Initialize approved fixture ledger")
    git(source, "push", "origin", "world-finance-daily")
    copies = []
    for name in ("one", "two"):
        target = tmp_path / name
        git(tmp_path, "clone", "--branch", "world-finance-daily", str(remote), str(target))
        copies.append(target)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("WORLD_FINANCE_ENVIRONMENT", "world-finance-production")
    monkeypatch.setenv("WORLD_FINANCE_STATE_TOKEN", "fake-state")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    def local_run(command, **kwargs):
        command = [str(remote) if x == "https://github.com/example/repo.git" else x
                   for x in command]
        return real_run(command, **kwargs)
    monkeypatch.setattr(world.subprocess, "run", local_run)
    from ai_daily.world_translation_budget import make_manifest, reserve
    grants = []
    for i, path in enumerate(copies):
        store = StateStore(path / "world-state")
        store.reserve_delivery("2026-11-17", f"attempt-{i}", NOW)
        grants.append(reserve(store, make_manifest(["Trade"]), NOW, str(100+i), "1"))
    grants[0].confirm(world.checkpoint_state(copies[0]))
    with pytest.raises(RuntimeError):
        grants[1].confirm(world.checkpoint_state(copies[1]))
    assert grants[0].consume("Trade", NOW)
    assert not grants[1].consume("Trade", NOW)


def test_protection_permission_failure_has_safe_numeric_diagnostic(monkeypatch):
    from ai_daily.world_translation_budget import BudgetUnavailable
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("WORLD_FINANCE_STATE_TOKEN", "fake-state")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        403, json={"message": "private token and headers must not be logged"})))
    with pytest.raises(BudgetUnavailable, match="^budget_guard_http_403$"):
        world.verify_append_only_protection(client=client)


def test_budget_diagnostic_rejects_unknown_exception_content():
    from ai_daily.world_translation_budget import BudgetUnavailable, safe_error_code
    assert safe_error_code(RuntimeError("private signed response")) == "unavailable"
    assert safe_error_code(BudgetUnavailable("fake-secret")) == "unavailable"
    assert safe_error_code(BudgetUnavailable("budget_guard_http_403")) == "budget_guard_http_403"
