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
