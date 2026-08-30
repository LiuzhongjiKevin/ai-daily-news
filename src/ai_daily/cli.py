"""Safe command-line entry points for collection, previews, and source diagnosis."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from ai_daily.pipeline import (
    DailyPipeline,
    DeliveryNoSendError,
    NoUsableDigestDataError,
    RunOptions,
    _timezone,
)
from ai_daily.state import DeliveryStateError, StateStore

AI_MODES = ("full", "economy", "off")
_SEND_SECRETS = ("MS_CLIENT_ID", "MS_TOKEN_KEY", "OUTLOOK_SENDER", "MAIL_TO")
_SUMMARY_SECRET_NAMES = ("DEEPSEEK_API_KEY", *_SEND_SECRETS, "GITHUB_TOKEN")
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_OAUTH_FIELD_PATTERN = re.compile(
    r"\b(?:access_token|refresh_token|id_token|client_secret|client_id|token_type|"
    r"expires_in|authorization|bearer)\b",
    re.IGNORECASE,
)


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


def _current_local_time() -> datetime:
    return datetime.now(_timezone("Asia/Shanghai"))


def _write_step_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _sanitize_summary_warning(value: object) -> str:
    text = " ".join(str(value).split())
    if _OAUTH_FIELD_PATTERN.search(text):
        # Free-form provider JSON is not a trusted diagnostic shape. A fixed marker is safer
        # than trying to parse every quoting, escaping, whitespace, or delimiter variant.
        return "<redacted-oauth-diagnostic>"
    for name in _SUMMARY_SECRET_NAMES:
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "<redacted>")
    text = _EMAIL_PATTERN.sub("<redacted-email>", text)
    return text.replace("|", "\\|")[:300]


def _write_run_summary(result: object | None, delivery_outcome: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    def metric(name: str, default: object) -> object:
        return getattr(result, name, default) if result is not None else default

    source_successes = metric("source_successes", 0)
    source_total = metric("source_total", 0)
    source_rate = metric("source_success_rate", None)
    source_text = (
        f"{source_successes}/{source_total} ({float(source_rate):.1f}%)"
        if source_rate is not None
        else f"{source_successes}/{source_total} (not run)"
    )
    github_status = str(metric("github_status", "not_run"))
    github_date = metric("github_data_date", None)
    github_text = f"{github_status} ({github_date})" if github_date else github_status
    completeness = (
        "lower bound" if bool(metric("cost_is_lower_bound", False)) else "complete"
    )
    currency = str(metric("cost_currency", "USD"))
    lines = [
        "## AI Daily run summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Beijing date | {metric('local_date', 'unavailable')} |",
        f"| Source success | {source_text} |",
        f"| Candidates | {metric('news_candidate_count', 0)} |",
        f"| Final news | {metric('final_news_count', 0)} |",
        f"| Repositories | {metric('repository_count', 0)} |",
        f"| GitHub data | {github_text} |",
        f"| Effective AI mode | {metric('effective_ai_mode', 'not_run')} |",
        f"| AI calls | {metric('ai_call_count', 0)} |",
        (
            "| Known tokens (cache hit / miss / output) | "
            f"{metric('input_cache_hit_tokens', 0)} / "
            f"{metric('input_cache_miss_tokens', 0)} / {metric('output_tokens', 0)} |"
        ),
        f"| Token/cost completeness | {completeness} |",
        f"| Current cost | {currency} {metric('estimated_cost', Decimal(0))} |",
        (
            "| 30-day projection | "
            f"{currency} {metric('thirty_day_projection', Decimal(0))} |"
        ),
        f"| Mail outcome | {delivery_outcome} |",
    ]
    warnings = list(metric("warnings", []) or [])
    if warnings:
        lines.extend(["", "### Sanitized warnings", ""])
        lines.extend(f"- {_sanitize_summary_warning(warning)}" for warning in warnings)
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _delivery_store() -> StateStore:
    return StateStore(project_root() / "data")


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
        f"| `{_sanitize_summary_warning(source)}` | "
        f"{'OK' if failure is None else 'FAILED: ' + _sanitize_summary_warning(failure)} |"
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
            command.add_argument("--attempt-id")
    validate = commands.add_parser("validate-sources")
    validate.add_argument("--minimum-success", type=int, default=80)
    reserve = commands.add_parser("reserve-delivery")
    reserve.add_argument("--attempt-id", required=True)
    reserve.add_argument("--force", action="store_true")
    release = commands.add_parser("release-delivery")
    release.add_argument("--attempt-id", required=True)
    release.add_argument("--date")
    resolve = commands.add_parser("resolve-delivery")
    resolve.add_argument("resolution", choices=("retry", "sent"))
    resolve.add_argument("--date")
    resolve.add_argument("--message-id")
    resolve.add_argument("--confirm-owner-terminal", action="store_true")
    return parser


def _run_command(args: argparse.Namespace) -> int:
    pipeline: DailyPipeline | None = None
    try:
        send = args.command == "run" and args.send
        mode = args.ai_mode or _settings_mode()
        if send and not _validate_send_secrets(mode):
            return 2
        pipeline = build_pipeline()
        result = pipeline.run(
            RunOptions(
                send=send,
                force=args.force,
                ai_mode=args.ai_mode,
                attempt_id=getattr(args, "attempt_id", None),
            )
        )
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
    except (
        DeliveryNoSendError,
        MailAuthError,
        MailSendError,
        NoUsableDigestDataError,
        OSError,
        ValidationError,
    ):
        print("Run failed: required service or local state is unavailable.", file=sys.stderr)
        return 1
    else:
        if result.already_sent:
            print(f"Already-sent no-op: {result.local_date}.")
        elif result.sent:
            print(f"Send accepted/queued: {result.local_date}; id={result.message_id}.")
        else:
            print(f"Preview generated: {result.local_date}; archive={result.markdown_path}.")
        return 0
    finally:
        outcome = pipeline.delivery_outcome if pipeline is not None else "not_attempted"
        _write_step_output("delivery_outcome", outcome)
        _write_run_summary(pipeline.last_result if pipeline is not None else None, outcome)


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
    failed = [
        _sanitize_summary_warning(source)
        for source, failure in outcomes
        if failure is not None
    ]
    print(f"Source validation: {successes}/{len(outcomes)} ({percent:.1f}%).")
    if failed:
        print("Failed sources: " + ", ".join(failed) + ".")
    return 0 if percent >= args.minimum_success else 1


def _delivery_command(args: argparse.Namespace) -> int:
    if args.command == "resolve-delivery" and not args.confirm_owner_terminal:
        print(
            "Delivery resolution requires confirmation that the owning run is terminal.",
            file=sys.stderr,
        )
        return 2
    try:
        local_date = getattr(args, "date", None) or _current_local_time().date().isoformat()
        store = _delivery_store()
        if args.command == "reserve-delivery":
            status = store.reserve_delivery(
                local_date,
                args.attempt_id,
                _current_local_time(),
                force=args.force,
            )
            _write_step_output("reservation_status", status)
            print(f"Delivery reservation: {status}.")
            return 0
        if args.command == "release-delivery":
            status = store.release_delivery(local_date, args.attempt_id)
            _write_step_output("release_status", status)
            print(f"Delivery cleanup: {status}.")
            return 0
        status = store.resolve_delivery(local_date, args.resolution, args.message_id)
        print(f"Delivery ambiguity resolved as {status}.")
        return 0
    except (DeliveryStateError, OSError, ValidationError):
        print("Delivery state command failed: input or local state is unavailable.", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-sources":
        return _validate_command(args)
    if args.command in {"reserve-delivery", "release-delivery", "resolve-delivery"}:
        return _delivery_command(args)
    return _run_command(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
