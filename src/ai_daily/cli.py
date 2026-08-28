"""Safe command-line entry points for collection, previews, and source diagnosis."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from openai import OpenAI
from pydantic import ValidationError

from ai_daily.ai import AIEnricher, AIEnrichmentError, OffEnricher
from ai_daily.collectors import build_collector_registry, load_sources
from ai_daily.collectors.base import SourceConfig, format_source_failure
from ai_daily.config import AppSettings, load_prices, load_settings
from ai_daily.http import RetryingClient
from ai_daily.mail import GraphMailer, MailAuthError, MailSendError
from ai_daily.pipeline import DailyPipeline, NoUsableDigestDataError, RunOptions
from ai_daily.state import StateStore

AI_MODES = ("full", "economy", "off")
_SEND_SECRETS = ("MS_CLIENT_ID", "MS_TOKEN_KEY", "OUTLOOK_SENDER", "MAIL_TO")


class ConfigurationError(RuntimeError):
    """Repository layout or validated runtime configuration is unavailable."""


class RepositoryLayoutError(ConfigurationError):
    """The command is running outside its supported editable repository checkout."""


class _LazyEnricher:
    """Create the paid client only when a non-off run actually needs it."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.off = OffEnricher()

    def enrich(self, news: object, repos: object, mode: Literal["full", "economy", "off"]):
        if mode == "off":
            return self.off.enrich(news, repos, mode)  # type: ignore[arg-type]
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise AIEnrichmentError("AI configuration is unavailable")
        client = OpenAI(api_key=key, base_url=str(self.settings.ai_base_url))
        return AIEnricher(client, self.settings).enrich(news, repos, mode)  # type: ignore[arg-type]


class _LazyMailer:
    """Avoid requiring Microsoft configuration until an explicit send is requested."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path

    def send(self, rendered: object) -> str:
        return GraphMailer.from_environment(self.cache_path).send(rendered)  # type: ignore[arg-type]


def project_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / "config" / "settings.yaml").is_file() or not (root / "templates").is_dir():
        raise RepositoryLayoutError(
            "Run ai-daily from an editable repository checkout with config/ and templates/."
        )
    return root


def _load_settings(root: Path) -> AppSettings:
    try:
        return load_settings(root)
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        raise ConfigurationError("Runtime settings are unavailable") from error


def _load_prices(root: Path):
    try:
        return load_prices(root)
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        raise ConfigurationError("Runtime prices are unavailable") from error


def _load_sources(root: Path) -> list[SourceConfig]:
    try:
        return load_sources(root / "config" / "sources.yaml")
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        raise ConfigurationError("Runtime sources are unavailable") from error


def required_send_secret_names(mode: str) -> tuple[str, ...]:
    """Return names only, never secret values, for one explicit delivery request."""
    return (("DEEPSEEK_API_KEY",) if mode != "off" else ()) + _SEND_SECRETS


def _settings_mode() -> str:
    return _load_settings(project_root()).ai_mode


def _validate_send_secrets(mode: str) -> bool:
    missing = [name for name in required_send_secret_names(mode) if not os.environ.get(name)]
    if not missing:
        return True
    print("Send configuration is unavailable: " + ", ".join(missing) + ".", file=sys.stderr)
    return False


def build_pipeline() -> DailyPipeline:
    """Wire the existing typed services without authenticating or sending during construction."""
    root = project_root()
    settings = _load_settings(root)
    client = RetryingClient()
    return DailyPipeline(
        settings=settings,
        prices=_load_prices(root),
        state_store=StateStore(root / "data"),
        sources=_load_sources(root),
        collector_registry=build_collector_registry(client),
        github_client=client,
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        enricher=_LazyEnricher(settings),
        off_enricher=OffEnricher(),
        mailer=_LazyMailer(root / "data" / "microsoft-token.enc"),
        templates_dir=root / "templates",
        output_root=root,
    )


def validate_collected_items(source: SourceConfig, items: list[object]) -> None:
    """Treat empty parsers as failures where that would mask a broken source adapter."""
    from ai_daily.collectors.base import PageParseError, SourceParseError

    if source.kind == "page" and not items:
        raise PageParseError("page returned zero valid cards")
    if source.kind in {"discovery", "github_releases"} and not items:
        raise SourceParseError(f"{source.kind} returned zero valid items")


def validate_sources() -> list[tuple[str, str | None]]:
    """Return a per-enabled-source result without making a single outage fatal to the report."""
    root = project_root()
    client = RetryingClient(max_attempts=1)
    registry = build_collector_registry(client)
    since = datetime.now(UTC) - timedelta(days=90)
    outcomes: list[tuple[str, str | None]] = []
    for source in _load_sources(root):
        try:
            items = registry.collectors[source.kind].collect(source, since)
            validate_collected_items(source, items)
        except Exception as error:  # noqa: BLE001 - the report intentionally includes every source
            outcomes.append((source.id, format_source_failure(source, error)))
        else:
            outcomes.append((source.id, None))
    return outcomes


def _write_source_summary(outcomes: list[tuple[str, str | None]], threshold: int) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    successes = sum(failure is None for _, failure in outcomes)
    percent = (100 * successes / len(outcomes)) if outcomes else 0
    lines = [
        "## AI Daily source validation",
        "",
        f"- Successful enabled sources: {successes}/{len(outcomes)} ({percent:.1f}%)",
        f"- Required minimum: {threshold}%",
        "",
        "| Source | Result |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| `{source}` | {'OK' if failure is None else 'FAILED: ' + failure} |"
        for source, failure in outcomes
    )
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-daily")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "preview"):
        command = commands.add_parser(name)
        command.add_argument("--ai-mode", choices=AI_MODES)
        command.add_argument("--force", action="store_true")
        if name == "run":
            command.add_argument("--send", action="store_true")
    validate = commands.add_parser("validate-sources")
    validate.add_argument("--minimum-success", type=int, default=80)
    return parser


def _run_command(args: argparse.Namespace) -> int:
    try:
        send = args.command == "run" and args.send
        mode = args.ai_mode or _settings_mode()
        if send and not _validate_send_secrets(mode):
            return 2
        result = build_pipeline().run(RunOptions(send=send, force=args.force, ai_mode=args.ai_mode))
    except RepositoryLayoutError:
        print(
            "Run failed: run from a checked-out repository installed with "
            "python -m pip install -e .",
            file=sys.stderr,
        )
        return 1
    except ConfigurationError:
        print("Run failed: runtime configuration is unavailable.", file=sys.stderr)
        return 1
    except (MailAuthError, MailSendError, NoUsableDigestDataError, OSError):
        print("Run failed: required service or local state is unavailable.", file=sys.stderr)
        return 1
    if result.already_sent:
        print(f"Already-sent no-op: {result.local_date}.")
    elif result.sent:
        print(f"Send accepted/queued: {result.local_date}; id={result.message_id}.")
    else:
        print(f"Preview generated: {result.local_date}; archive={result.markdown_path}.")
    return 0


def _validate_command(args: argparse.Namespace) -> int:
    if not 0 <= args.minimum_success <= 100:
        print("minimum-success must be between 0 and 100.", file=sys.stderr)
        return 2
    try:
        outcomes = validate_sources()
        _write_source_summary(outcomes, args.minimum_success)
    except (ConfigurationError, OSError):
        print("Source validation could not run.", file=sys.stderr)
        return 1
    successes = sum(failure is None for _, failure in outcomes)
    percent = (100 * successes / len(outcomes)) if outcomes else 0
    failed = [source for source, failure in outcomes if failure is not None]
    print(f"Source validation: {successes}/{len(outcomes)} ({percent:.1f}%).")
    if failed:
        print("Failed sources: " + ", ".join(failed) + ".")
    return 0 if percent >= args.minimum_success else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _validate_command(args) if args.command == "validate-sources" else _run_command(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
