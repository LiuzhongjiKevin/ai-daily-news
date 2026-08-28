from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

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


class FakePipeline:
    def __init__(self, result: FakeResult | None = None) -> None:
        self.result = result or FakeResult()
        self.last_options = None

    def run(self, options: object) -> FakeResult:
        self.last_options = options
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


def test_send_secret_requirements_exclude_deepseek_for_off_mode() -> None:
    """Would catch off-mode sending needlessly demanding a paid AI secret."""
    from ai_daily.cli import required_send_secret_names

    assert required_send_secret_names("off") == (
        "MS_CLIENT_ID",
        "MS_TOKEN_KEY",
        "OUTLOOK_SENDER",
        "MAIL_TO",
    )
    assert required_send_secret_names("full")[0] == "DEEPSEEK_API_KEY"


def test_validate_sources_threshold_writes_redacted_markdown_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Would catch a failed enabled source being hidden or a passed threshold exiting nonzero."""
    from ai_daily.cli import main

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        "ai_daily.cli.validate_sources",
        lambda: [("official", None), ("private", "HTTP 503 url=https://x.test?<redacted>")],
    )

    assert main(["validate-sources", "--minimum-success", "50"]) == 0
    output = capsys.readouterr().out
    assert "1/2" in output
    assert "private" in summary.read_text(encoding="utf-8")
    assert "<redacted>" in summary.read_text(encoding="utf-8")
    assert main(["validate-sources", "--minimum-success", "80"]) == 1


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
    assert "site-packages" not in capsys.readouterr().err
