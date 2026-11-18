from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from test_world_translation_budget import install, ledger

from ai_daily import world_finance as world
from ai_daily.state import StateStore
from ai_daily.world_alibaba import AlibabaTranslator

NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)


def setup_probe(monkeypatch, tmp_path):
    for name, value in {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/master", "GITHUB_RUN_ID": "456",
        "GITHUB_RUN_ATTEMPT": "1", "WORLD_ALIBABA_PROBE_APPROVED": "true",
        "WORLD_FINANCE_ENVIRONMENT": "world-finance-production",
        "WORLD_ALIBABA_TRANSLATION_ENABLED": "true",
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "fake-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "fake-secret",
    }.items():
        monkeypatch.setenv(name, value)
    store = StateStore(tmp_path / "world-state")
    install(store, ledger())
    monkeypatch.setattr(world, "translation_now", lambda: NOW)
    return store


def test_probe_reserves_six_units_before_one_request_without_mail(tmp_path, monkeypatch):
    from ai_daily.world_probe import probe

    store = setup_probe(monkeypatch, tmp_path)
    before = store.load_run_state().model_dump()
    events = []
    monkeypatch.setattr(world, "verify_budget_history", lambda root: events.append("verify"))

    def checkpoint(root):
        budget = store.load_run_state().translation_budget["months"]["2026-11"]
        assert budget["reserved"] == 6
        assert budget["reservations"]["456"]["characters"] == 6
        events.append("checkpoint")
        return "a" * 40

    monkeypatch.setattr(world, "checkpoint_state", checkpoint)
    for name in ("collect", "world_mailer", "deliver"):
        monkeypatch.setattr(world, name, lambda *a, **k: pytest.fail("No mail or feeds"))

    def handler(request):
        assert events == ["verify", "checkpoint"]
        events.append("request")
        assert parse_qs(request.content.decode())["SourceText"] == ["Hello."]
        return httpx.Response(200, json={"Code": 200, "Data": {"Translated": "\u4f60\u597d\u3002"}})

    real_init = AlibabaTranslator.__init__
    monkeypatch.setattr(AlibabaTranslator, "__init__", lambda self, *a, **k: real_init(
        self, *a, **k, client=httpx.Client(transport=httpx.MockTransport(handler))))
    assert probe(tmp_path) == {"status": "translated", "reserved_characters": 6, "requests": 1}
    after = store.load_run_state().model_dump()
    assert {k: v for k, v in after.items() if k != "translation_budget"} == {
        k: v for k, v in before.items() if k != "translation_budget"}
    assert events == ["verify", "checkpoint", "request"]


def test_launcher_probe_is_manual_mutually_exclusive_and_has_no_mail_secrets():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
                              ".github/workflows/world-finance.yml").read_text("utf-8"))
    # PyYAML's YAML 1.1 parser maps the GitHub 'on' key to True.
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]
    assert dispatch["translation_probe"]["default"] is False
    probe_job = workflow["jobs"]["translation_probe"]
    condition = probe_job["if"]
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.ref == 'refs/heads/master'" in condition
    assert "inputs.translation_probe == true" in condition
    assert "inputs.send != true" in condition
    assert "inputs.translation_probe != true" in workflow["jobs"]["send"]["if"]
    assert workflow["concurrency"] == {
        "group": "world-finance-delivery", "cancel-in-progress": False}
    assert probe_job["needs"] == "preview"
    assert probe_job["environment"] == "world-finance-production"
    checkout = probe_job["steps"][0]["with"]
    assert checkout["ref"] == "${{ needs.preview.outputs.source_sha }}"
    assert checkout["fetch-depth"] == 0
    step = next(s for s in probe_job["steps"] if s.get("run") ==
                "python -m ai_daily.world_probe")
    assert step["env"]["WORLD_ALIBABA_PROBE_APPROVED"] == "true"
    assert step["env"]["WORLD_ALIBABA_TRANSLATION_ENABLED"] == "true"
    assert not any(name in str(probe_job) for name in (
        "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO", "TENCENTCLOUD"))
    preview = workflow["jobs"]["preview"]
    assert "secrets." not in str(preview)
    feeds = next(s for s in preview["steps"] if s.get("run") ==
                 "python -m ai_daily.world_finance preview")
    assert feeds["if"] == "inputs.translation_probe != true"


@pytest.mark.parametrize("name,value", [
    ("GITHUB_ACTIONS", "false"), ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_REF", "refs/heads/other"), ("WORLD_FINANCE_ENVIRONMENT", "other"),
    ("WORLD_ALIBABA_PROBE_APPROVED", "false"),
    ("WORLD_ALIBABA_TRANSLATION_ENABLED", "false"),
    ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""),
])
def test_unapproved_probe_stops_before_history_or_reservation(tmp_path, monkeypatch, name, value):
    from ai_daily.world_probe import probe
    from ai_daily.world_translation_budget import BudgetUnavailable

    setup_probe(monkeypatch, tmp_path)
    state_path = tmp_path / "world-state" / "state.json"
    before = state_path.read_bytes()
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(world, "verify_budget_history", lambda root: pytest.fail("No history call"))
    with pytest.raises(BudgetUnavailable):
        probe(tmp_path)
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("problem", ["missing", "corrupt", "exhausted", "history", "month"])
def test_unverified_probe_budget_never_reaches_checkpoint(tmp_path, monkeypatch, problem):
    from ai_daily.world_probe import probe
    from ai_daily.world_translation_budget import BudgetUnavailable

    store = setup_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(world, "verify_budget_history", lambda root: None)
    if problem in {"missing", "corrupt", "exhausted"}:
        install(store, {"missing": None, "corrupt": {}, "exhausted": ledger(baseline=299995)}[
            problem])
    elif problem == "history":
        def fail_history(root):
            raise BudgetUnavailable("budget_history_unverified")
        monkeypatch.setattr(world, "verify_budget_history", fail_history)
    else:
        monkeypatch.setattr(world, "translation_now", lambda: NOW.replace(
            day=30, hour=15, minute=59, second=30))
    monkeypatch.setattr(world, "checkpoint_state", lambda root: pytest.fail("No checkpoint"))
    monkeypatch.setattr(AlibabaTranslator, "__init__", lambda *a, **k: pytest.fail("No provider"))
    with pytest.raises(BudgetUnavailable):
        probe(tmp_path)


@pytest.mark.parametrize("receipt", [None, "", "unknown", "raise"])
def test_probe_checkpoint_failure_keeps_reservation_and_never_calls_provider(
        tmp_path, monkeypatch, receipt):
    from ai_daily.world_probe import probe

    store = setup_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(world, "verify_budget_history", lambda root: None)
    def checkpoint(root):
        if receipt == "raise":
            raise RuntimeError("push outcome unknown")
        return receipt
    monkeypatch.setattr(world, "checkpoint_state", checkpoint)
    monkeypatch.setattr(AlibabaTranslator, "__init__", lambda *a, **k: pytest.fail("No provider"))
    with pytest.raises(RuntimeError):
        probe(tmp_path)
    assert store.load_run_state().translation_budget["months"]["2026-11"]["reserved"] == 6


@pytest.mark.parametrize("failure", ["timeout", "business", "malformed", "cross_month"])
def test_failed_probe_never_retries_or_refunds_or_reuses_run(
        tmp_path, monkeypatch, capsys, failure):
    from ai_daily.world_probe import probe
    from ai_daily.world_translation_budget import BudgetUnavailable

    store = setup_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(world, "verify_budget_history", lambda root: None)
    checkpoints, requests = [], []
    def checkpoint(root):
        checkpoints.append(True)
        if failure == "cross_month":
            monkeypatch.setattr(world, "translation_now", lambda: NOW.replace(month=12, day=1))
        return "a" * 40
    monkeypatch.setattr(world, "checkpoint_state", checkpoint)
    def handler(request):
        requests.append(True)
        if failure == "timeout":
            raise httpx.ReadTimeout("fake-secret private response")
        if failure == "business":
            return httpx.Response(200, json={"Code": 10013, "Message": "fake-secret private"})
        return httpx.Response(200, json={"Code": 200, "Data": []})
    real_init = AlibabaTranslator.__init__
    monkeypatch.setattr(AlibabaTranslator, "__init__", lambda self, *a, **k: real_init(
        self, *a, **k, client=httpx.Client(transport=httpx.MockTransport(handler))))
    result = probe(tmp_path)
    assert result["status"] == "unverified"
    assert len(requests) == (0 if failure == "cross_month" else 1)
    assert store.load_run_state().translation_budget["months"]["2026-11"]["reserved"] == 6
    monkeypatch.setattr(world, "translation_now", lambda: NOW)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(BudgetUnavailable):
        probe(tmp_path)
    assert len(checkpoints) == 1
    output = capsys.readouterr().out
    assert not any(value in output for value in ("fake-secret", "fake-id", "private", "Hello."))


def test_probe_cli_returns_failure_without_exception_details(monkeypatch, capsys):
    from ai_daily import world_probe

    def fail(root):
        raise RuntimeError("fake-secret signed response")
    monkeypatch.setattr(world_probe, "probe", fail)
    assert world_probe.main() == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "fake-secret" not in output
