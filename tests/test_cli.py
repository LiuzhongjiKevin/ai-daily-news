from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import pytest


@dataclass
class FakeResult:
    local_date: str = "2026-08-24"
    sent: bool = False
    already_sent: bool = False
    message_id: str | None = None
    markdown_path: Path | None = Path("digests/2026-08-24.md")
    warnings: list[str] | None = None
    estimated_cost: Decimal = Decimal(0)
    delivery_outcome: str = "not_attempted"
    source_successes: int = 0
    source_total: int = 0
    source_success_rate: float | None = None
    news_candidate_count: int = 0
    final_news_count: int = 0
    repository_count: int = 0
    github_status: str = "not_run"
    github_data_date: str | None = None
    effective_ai_mode: str | None = None
    ai_call_count: int = 0
    token_usage_complete: bool = True
    cost_is_lower_bound: bool = False
    input_cache_hit_tokens: int = 0
    input_cache_miss_tokens: int = 0
    output_tokens: int = 0
    cost_currency: str = "USD"
    thirty_day_projection: Decimal = Decimal(0)


class FakePipeline:
    def __init__(self, result: FakeResult | None = None) -> None:
        self.result = result or FakeResult()
        self.last_options = None
        self.delivery_outcome = "not_attempted"
        self.last_result = self.result

    def run(self, options: object) -> FakeResult:
        self.last_options = options
        self.delivery_outcome = self.result.delivery_outcome
        return self.result


def test_preview_never_sends_and_allows_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Would catch preview forwarding a send request into the delivery pipeline."""
    from ai_daily.cli import main

    pipeline = FakePipeline()
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: pipeline)

    assert main(["preview", "--ai-mode", "off", "--force"]) == 0
    assert pipeline.last_options.send is False
    assert pipeline.last_options.ai_mode == "off"
    assert pipeline.last_options.force is True


def test_run_sends_only_after_required_secrets_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Would catch a send path constructing mail or AI clients with missing credentials."""
    from ai_daily.cli import main

    pipeline = FakePipeline(FakeResult(sent=True, message_id="accepted-safe-id"))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: pipeline)
    monkeypatch.setattr("ai_daily.cli.required_send_secret_names", lambda mode: ("MS_CLIENT_ID",))
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)

    assert main(["run", "--send", "--ai-mode", "off"]) == 2
    assert pipeline.last_options is None

    monkeypatch.setenv("MS_CLIENT_ID", "configured")
    assert main(["run", "--send", "--force", "--ai-mode", "off"]) == 0
    assert pipeline.last_options.send is True
    assert pipeline.last_options.force is True


def test_send_secret_requirements_never_make_optional_ai_key_a_mail_prerequisite() -> None:
    """Would catch full/economy sends aborting before deterministic AI fallback."""
    from ai_daily.cli import required_send_secret_names

    expected = (
        "MS_CLIENT_ID",
        "MS_TOKEN_KEY",
        "OUTLOOK_SENDER",
        "MAIL_TO",
    )
    assert required_send_secret_names("off") == expected
    assert required_send_secret_names("economy") == expected
    assert required_send_secret_names("full") == expected


def test_full_send_without_deepseek_key_reaches_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would catch a missing optional AI key blocking an otherwise configured delivery."""
    from ai_daily.cli import main

    pipeline = FakePipeline(FakeResult(sent=True, message_id="accepted-safe-id"))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: pipeline)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    for name in ("MS_CLIENT_ID", "MS_TOKEN_KEY", "OUTLOOK_SENDER", "MAIL_TO"):
        monkeypatch.setenv(name, "configured")

    assert main(["run", "--send", "--ai-mode", "full"]) == 0
    assert pipeline.last_options.send is True
    assert pipeline.last_options.ai_mode == "full"


def test_lazy_paid_client_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would catch hidden SDK retries bypassing application attempt and usage accounting."""
    from ai_daily.cli import _LazyEnricher
    from ai_daily.config import AppSettings

    captured: dict[str, object] = {}

    def fake_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "configured")
    monkeypatch.setattr("ai_daily.cli.OpenAI", fake_openai)

    news, repos, usage = _LazyEnricher(AppSettings()).enrich([], [], "full")

    assert news == []
    assert repos == []
    assert usage == []
    assert captured == {
        "api_key": "configured",
        "base_url": "https://api.deepseek.com",
        "max_retries": 0,
    }


def test_validate_sources_threshold_writes_redacted_markdown_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a failed enabled source being hidden or a passed threshold exiting nonzero."""
    from ai_daily.cli import main

    summary = tmp_path / "summary.md"
    secret = "source-validation-secret"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("AI_DAILY_STATE_TOKEN", secret)
    monkeypatch.setattr(
        "ai_daily.cli.validate_sources",
        lambda: [
            ("official", None),
            (
                "private-reader@example.test",
                (
                    "HTTP 503 url=https://x.test?<redacted> "
                    f"reader@example.test access_token=oauth-value {secret}"
                ),
            ),
        ],
    )

    assert main(["validate-sources", "--minimum-success", "50"]) == 0
    output = capsys.readouterr().out
    assert "1/2" in output
    assert "<redacted-email>" in summary.read_text(encoding="utf-8")
    assert "reader@example.test" not in output
    summary_text = summary.read_text(encoding="utf-8")
    assert "<redacted-oauth-diagnostic>" in summary_text
    assert secret not in summary_text
    assert "reader@example.test" not in summary_text
    assert "oauth-value" not in summary_text
    assert main(["validate-sources", "--minimum-success", "80"]) == 1


def test_validate_sources_counts_healthy_empty_as_passing_and_labels_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch quiet discovery/releases lowering the source-health threshold."""
    from ai_daily.cli import main

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        "ai_daily.cli.validate_sources",
        lambda: [
            ("official", None),
            ("quiet-release", "healthy_empty"),
            ("broken-page", "PageParseError: selector mismatch"),
        ],
    )

    assert main(["validate-sources", "--minimum-success", "66"]) == 0
    output = capsys.readouterr().out
    summary_text = summary.read_text(encoding="utf-8")
    assert "2/3 (66.7%)" in output
    assert "Successful: 1" in summary_text
    assert "Healthy empty: 1" in summary_text
    assert "Failed: 1" in summary_text
    assert "HEALTHY_EMPTY" in summary_text
    assert "Threshold: PASS (66% required)" in summary_text

    assert main(["validate-sources", "--minimum-success", "67"]) == 1


def test_validate_sources_keeps_valid_empty_and_malformed_schema_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Would catch a malformed discovery schema being folded into healthy emptiness."""
    from ai_daily.cli import validate_sources
    from ai_daily.collectors.base import SourceConfig

    sources = [
        SourceConfig(
            id="quiet-discovery",
            name="Quiet discovery",
            kind="discovery",
            source_type="discovery",
            url="https://quiet.test/discovery",
            language="en",
            category="industry",
            allowed_domains=["example.test"],
        ),
        SourceConfig(
            id="quiet-release",
            name="Quiet release",
            kind="github_releases",
            source_type="release",
            url="https://api.github.com/repos/acme/quiet/releases",
            language="en",
            category="open_source",
        ),
        SourceConfig(
            id="malformed-discovery",
            name="Malformed discovery",
            kind="discovery",
            source_type="discovery",
            url="https://malformed.test/discovery",
            language="en",
            category="industry",
            allowed_domains=["example.test"],
        ),
    ]

    class OfflineClient:
        def get(self, url: str, **_: object) -> httpx.Response:
            payload: object
            if "quiet.test" in url:
                payload = {"articles": []}
            elif "api.github.com" in url:
                payload = []
            else:
                payload = {"unexpected": []}
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr("ai_daily.cli.project_root", lambda: tmp_path)
    monkeypatch.setattr("ai_daily.cli._load_sources", lambda _: sources)
    monkeypatch.setattr("ai_daily.cli.RetryingClient", lambda **_: OfflineClient())

    outcomes = validate_sources()

    assert outcomes[:2] == [
        ("quiet-discovery", "healthy_empty"),
        ("quiet-release", "healthy_empty"),
    ]
    assert outcomes[2][0] == "malformed-discovery"
    assert outcomes[2][1] and outcomes[2][1].startswith(
        "SourceParseError: discovery response schema is invalid"
    )


def test_cli_reports_already_sent_without_recipient_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a duplicate no-op being reported as a new send or leaking address details."""
    from ai_daily.cli import main

    monkeypatch.setattr(
        "ai_daily.cli.build_pipeline", lambda: FakePipeline(FakeResult(already_sent=True))
    )
    assert main(["run", "--ai-mode", "off"]) == 0
    output = capsys.readouterr().out
    assert "Already-sent no-op" in output
    assert "@" not in output


def test_invalid_ai_mode_is_rejected_by_the_cli_parser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Would catch an unsupported mode reaching the delivery pipeline."""
    from ai_daily.cli import main

    with pytest.raises(SystemExit) as error:
        main(["preview", "--ai-mode", "invalid"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_configuration_failures_are_concise_redacted_and_programmer_errors_escape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch safe configuration failures leaking details or arbitrary bugs being swallowed."""
    from ai_daily.cli import ConfigurationError, main

    def missing_configuration() -> str:
        raise ConfigurationError("actual-secret-value")

    monkeypatch.setattr("ai_daily.cli._settings_mode", missing_configuration)
    assert main(["preview"]) == 1
    assert "actual-secret-value" not in capsys.readouterr().err

    monkeypatch.setattr("ai_daily.cli._settings_mode", lambda: "off")
    monkeypatch.setattr(
        "ai_daily.cli.build_pipeline", lambda: (_ for _ in ()).throw(AssertionError("programmer bug"))
    )
    with pytest.raises(AssertionError, match="programmer bug"):
        main(["preview"])


def test_source_validation_configuration_failure_is_concise_and_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a broken source configuration exposing its contents in CLI output."""
    from ai_daily.cli import ConfigurationError, main

    monkeypatch.setattr(
        "ai_daily.cli.validate_sources",
        lambda: (_ for _ in ()).throw(ConfigurationError("private-value")),
    )
    assert main(["validate-sources"]) == 1
    assert "private-value" not in capsys.readouterr().err


def test_noneditable_installation_fails_early_with_checkout_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a wheel installation guessing site-packages parents for repository assets."""
    from ai_daily import cli

    installed_module = tmp_path / "site-packages" / "ai_daily" / "cli.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.touch()
    monkeypatch.setattr(cli, "__file__", str(installed_module))

    with pytest.raises(cli.ConfigurationError, match="editable repository checkout"):
        cli.project_root()
    assert cli.main(["preview"]) == 1
    output = capsys.readouterr().err
    assert "python -m pip install -e ." in output
    assert "site-packages" not in output
    assert str(tmp_path) not in output


def test_send_attempt_id_is_forwarded_to_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Would catch a workflow reservation owner being dropped before delivery."""
    from ai_daily.cli import main

    pipeline = FakePipeline(FakeResult(sent=True, message_id="accepted-safe-id"))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: pipeline)
    monkeypatch.setattr("ai_daily.cli.required_send_secret_names", lambda mode: ())

    assert main(["run", "--send", "--ai-mode", "off", "--attempt-id", "gh-100-1"]) == 0
    assert pipeline.last_options.attempt_id == "gh-100-1"


def test_reserve_and_resolve_commands_change_only_safe_local_delivery_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch operator commands requiring network services or printing ownership values."""
    from ai_daily import cli
    from ai_daily.state import StateStore

    monkeypatch.setattr(cli, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_current_local_time", lambda: cli.datetime(2026, 8, 24, tzinfo=cli.UTC))
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert cli.main(["reserve-delivery", "--attempt-id", "gh-100-1"]) == 0
    assert "reservation_status=reserved" in output.read_text("utf-8")
    assert "gh-100-1" not in capsys.readouterr().out
    state = StateStore(tmp_path / "data").load_run_state()
    assert state.delivery_intents["2026-08-24"].attempt_id == "gh-100-1"

    state.delivery_intents["2026-08-24"].status = "ambiguous"
    StateStore(tmp_path / "data").save_run_state(state)
    assert (
        cli.main(
            [
                "resolve-delivery",
                "retry",
                "--date",
                "2026-08-24",
                "--confirm-owner-terminal",
            ]
        )
        == 0
    )
    assert StateStore(tmp_path / "data").load_run_state().delivery_intents == {}


def test_resolution_requires_explicit_terminal_owner_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a local operator resolving while the owning sender may still be active."""
    from ai_daily import cli
    from ai_daily.state import StateStore

    monkeypatch.setattr(cli, "project_root", lambda: tmp_path)
    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", cli.datetime(2026, 8, 24, tzinfo=cli.UTC))

    assert cli.main(["resolve-delivery", "retry", "--date", "2026-08-24"]) == 2
    assert "terminal" in capsys.readouterr().err.lower()
    assert "2026-08-24" in store.load_run_state().delivery_intents


def test_resolution_rejects_unsafe_inputs_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch validation errors leaking a full address or secret-like message fingerprint."""
    from ai_daily import cli

    unsafe = "reader@example.test"
    monkeypatch.setattr(cli, "project_root", lambda: tmp_path)

    assert cli.main(["reserve-delivery", "--attempt-id", unsafe]) == 2
    assert unsafe not in capsys.readouterr().err
    assert (
        cli.main(
            [
                "resolve-delivery",
                "sent",
                "--date",
                "2026-08-24",
                "--message-id",
                unsafe,
                "--confirm-owner-terminal",
            ]
        )
        == 2
    )
    assert unsafe not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("result", "failure", "expected_code", "expected_outcome"),
    [
        (FakeResult(sent=True, delivery_outcome="accepted"), None, 0, "accepted"),
        (None, "pre-mail", 1, "not_attempted"),
        (None, "mail", 1, "ambiguous"),
    ],
)
def test_run_writes_delivery_outcome_after_controlled_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: FakeResult | None,
    failure: str | None,
    expected_code: int,
    expected_outcome: str,
) -> None:
    """Would catch failure cleanup guessing from stale or prematurely written runner output."""
    from ai_daily.cli import main
    from ai_daily.mail import MailSendError
    from ai_daily.pipeline import NoUsableDigestDataError

    class OutcomePipeline(FakePipeline):
        def run(self, options: object) -> FakeResult:
            self.last_options = options
            if failure == "pre-mail":
                self.delivery_outcome = "not_attempted"
                raise NoUsableDigestDataError("safe failure")
            if failure == "mail":
                self.delivery_outcome = "ambiguous"
                raise MailSendError("safe failure")
            assert result is not None
            self.delivery_outcome = result.delivery_outcome
            self.last_result = result
            return result

    output = tmp_path / "github-output.txt"
    pipeline = OutcomePipeline(result)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: pipeline)
    monkeypatch.setattr("ai_daily.cli.required_send_secret_names", lambda mode: ())

    assert main(["run", "--send", "--ai-mode", "off"]) == expected_code
    assert output.read_text("utf-8").splitlines() == [
        f"delivery_outcome={expected_outcome}"
    ]


def test_run_appends_complete_sanitized_github_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Would catch required daily metrics being absent or secret-bearing warnings entering Summary."""
    from ai_daily.cli import main

    secret = "configured-secret-value"
    result = FakeResult(
        source_successes=7,
        source_total=8,
        source_success_rate=87.5,
        news_candidate_count=6,
        final_news_count=4,
        repository_count=10,
        github_status="cached",
        github_data_date="2026-08-23",
        effective_ai_mode="off",
        ai_call_count=2,
        token_usage_complete=False,
        cost_is_lower_bound=True,
        input_cache_hit_tokens=11,
        input_cache_miss_tokens=22,
        output_tokens=33,
        estimated_cost=Decimal("0.001234"),
        thirty_day_projection=Decimal("0.037020"),
        delivery_outcome="ambiguous",
        warnings=[
            (
                f"reader@example.test access_token=oauth-value client_id=public-client "
                f"id_token=id-value {secret}"
            ),
            "safe degraded warning",
        ],
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: FakePipeline(result))

    assert main(["preview", "--ai-mode", "off"]) == 0
    content = summary.read_text("utf-8")
    for phrase in (
        "7/8 (87.5%)",
        "Candidates | 6",
        "Final news | 4",
        "Repositories | 10",
        "cached (2026-08-23)",
        "Effective AI mode | off",
        "AI calls | 2",
        "11 / 22 / 33",
        "lower bound",
        "USD 0.001234",
        "USD 0.037020",
        "Mail outcome | ambiguous",
        "safe degraded warning",
    ):
        assert phrase in content
    assert secret not in content
    assert "reader@example.test" not in content
    assert "oauth-value" not in content
    assert "public-client" not in content
    assert "id-value" not in content


def test_summary_replaces_hostile_quoted_oauth_json_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Would catch quoted or escaped JSON OAuth values outgrowing token-style redaction."""
    from ai_daily.cli import main

    secrets = (
        "access secret with spaces,comma-tail",
        "refresh-secret-after-comma",
        "client-secret-value",
        "public-client-value",
        "bearer-secret.with.parts",
        "escaped access secret",
    )
    result = FakeResult(
        warnings=[
            (
                '{"error":{"access_token":"access secret with spaces,comma-tail",'
                '"refresh_token":"refresh-secret-after-comma",'
                '"client_secret":"client-secret-value",'
                '"client_id":"public-client-value",'
                '"authorization":"Bearer bearer-secret.with.parts"},'
                '"contact":"json-reader@example.test"}'
            ),
            r'{\"access_token\":\"escaped access secret\"}',
        ]
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: FakePipeline(result))

    assert main(["preview", "--ai-mode", "off"]) == 0
    content = summary.read_text("utf-8")
    assert content.count("<redacted-oauth-diagnostic>") == 2
    for secret in secrets:
        assert secret not in content
    assert "json-reader@example.test" not in content


def test_ambiguous_failure_still_appends_typed_run_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Would catch failed mail calls losing their operator-visible ambiguous outcome."""
    from ai_daily.cli import main
    from ai_daily.mail import MailSendError

    result = FakeResult(
        delivery_outcome="ambiguous",
        github_status="current",
        github_data_date="2026-08-24",
        final_news_count=1,
    )

    class AmbiguousPipeline(FakePipeline):
        def run(self, options: object) -> FakeResult:
            self.delivery_outcome = "ambiguous"
            self.last_result = result
            raise MailSendError("safe failure")

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("ai_daily.cli.build_pipeline", lambda: AmbiguousPipeline(result))
    monkeypatch.setattr("ai_daily.cli.required_send_secret_names", lambda mode: ())

    assert main(["run", "--send", "--ai-mode", "off"]) == 1
    content = summary.read_text("utf-8")
    assert "Mail outcome | ambiguous" in content
    assert "Final news | 1" in content
