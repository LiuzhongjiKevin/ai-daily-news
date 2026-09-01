"""End-to-end orchestration and deliberate degradation for daily digests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dateutil.tz import gettz
from pydantic import BaseModel, Field

from ai_daily.ai import AIEnrichmentError
from ai_daily.collectors.base import (
    CollectorRegistry,
    SourceCollectionBatch,
    SourceConfig,
    parse_datetime,
)
from ai_daily.collectors.github import (
    GitHubDataError,
    GitHubResponseError,
    GitHubSnapshotBatch,
    discover_candidates,
    fetch_repo_snapshots,
)
from ai_daily.config import AppSettings, PricingTable
from ai_daily.cost import CostReport, calculate_cost
from ai_daily.github_rank import (
    load_recent_snapshots_with_warnings,
    load_snapshot_safely,
    rank_repositories,
)
from ai_daily.models import (
    Digest,
    NewsCluster,
    RankedRepo,
    RepoSnapshot,
    RunState,
    SafeAttemptId,
    UsageRecord,
)
from ai_daily.news import prepare_news
from ai_daily.render import RenderedDigest, render_digest
from ai_daily.state import DeliveryIntentConflictError, DeliveryStateError, StateStore


class RunOptions(BaseModel):
    send: bool = False
    force: bool = False
    ai_mode: Literal["full", "economy", "off"] | None = None
    attempt_id: SafeAttemptId | None = None


class OutputPaths(BaseModel):
    markdown: Path
    html: Path
    text: Path
    cost_report: Path


class RunResult(BaseModel):
    local_date: str
    sent: bool
    already_sent: bool = False
    message_id: str | None = None
    markdown_path: Path | None = None
    warnings: list[str] = Field(default_factory=list)
    estimated_cost: Decimal = Decimal(0)
    usage: list[UsageRecord] = Field(default_factory=list)
    delivery_outcome: Literal["not_attempted", "ambiguous", "accepted"] = "not_attempted"
    source_successes: int = Field(default=0, ge=0)
    source_total: int = Field(default=0, ge=0)
    source_success_rate: float | None = Field(default=None, ge=0, le=100)
    news_candidate_count: int = Field(default=0, ge=0)
    final_news_count: int = Field(default=0, ge=0)
    repository_count: int = Field(default=0, ge=0)
    github_status: Literal["not_run", "current", "cached", "unavailable"] = "not_run"
    github_data_date: str | None = None
    effective_ai_mode: Literal["full", "economy", "off"] | None = None
    ai_call_count: int = Field(default=0, ge=0)
    token_usage_complete: bool = True
    cost_is_lower_bound: bool = False
    input_cache_hit_tokens: int = Field(default=0, ge=0)
    input_cache_miss_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_currency: str = "USD"
    thirty_day_projection: Decimal = Decimal(0)


class Clock(Protocol):
    def now(self, timezone: tzinfo) -> datetime: ...


class Enricher(Protocol):
    def enrich(
        self,
        news: list[NewsCluster],
        repos: list[RankedRepo],
        mode: Literal["full", "economy", "off"],
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]: ...


class Mailer(Protocol):
    def send(self, rendered: RenderedDigest) -> str: ...


DiscoverRepositories = Callable[[object, str, date, list[str]], list[str]]
FetchRepositories = Callable[[object, str, list[str], datetime], list[RepoSnapshot]]
Renderer = Callable[[Digest, CostReport, Path], RenderedDigest]


class GitHubCollectionError(RuntimeError):
    """A transport or typed-data failure prevents obtaining today's GitHub snapshot."""

    def __init__(self, message: str, *, warnings: list[str] | None = None) -> None:
        super().__init__(message)
        self.warnings = list(warnings or [])


class GitHubRankingBatch(list[RankedRepo]):
    """List-compatible rankings with reader-safe partial metadata warnings."""

    def __init__(
        self, rankings: list[RankedRepo], *, warnings: list[str] | None = None
    ) -> None:
        super().__init__(rankings)
        self.warnings = list(warnings or [])


class NoUsableDigestDataError(RuntimeError):
    """Neither digest section contains reader-safe data, so rendering must not continue."""


class DeliveryNoSendError(RuntimeError):
    """A typed safe failure prevents Graph from being called."""


class DeliveryReservationRequiredError(DeliveryNoSendError):
    """An externally owned send lacks its committed reservation."""


class DeliveryAmbiguousError(DeliveryNoSendError):
    """Unresolved delivery work requires mailbox inspection before another send."""


class SystemClock:
    def now(self, timezone: tzinfo) -> datetime:
        return datetime.now(timezone)


class DailyPipeline:
    """Coordinate typed services while keeping delivery state behind Graph acceptance."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        prices: PricingTable,
        state_store: StateStore,
        sources: list[SourceConfig],
        collector_registry: CollectorRegistry,
        github_client: object,
        github_token: str,
        enricher: Enricher,
        off_enricher: Enricher,
        mailer: Mailer,
        templates_dir: Path,
        output_root: Path,
        clock: Clock | None = None,
        discover_repositories: DiscoverRepositories = discover_candidates,
        fetch_repositories: FetchRepositories = fetch_repo_snapshots,
        renderer: Renderer = render_digest,
    ) -> None:
        self.settings = settings
        self.prices = prices
        self.state_store = state_store
        self.sources = sources
        self.collector_registry = collector_registry
        self.github_client = github_client
        self.github_token = github_token
        self.enricher = enricher
        self.off_enricher = off_enricher
        self.mailer = mailer
        self.templates_dir = templates_dir
        self.output_root = output_root
        self.clock = clock or SystemClock()
        self.discover_repositories = discover_repositories
        self.fetch_repositories = fetch_repositories
        self.renderer = renderer
        self._usage_for_cost_report: list[UsageRecord] = []
        self.delivery_outcome: Literal["not_attempted", "ambiguous", "accepted"] = (
            "not_attempted"
        )
        self.last_result: RunResult | None = None

    def collect_news(
        self, since: datetime | None, now: datetime
    ) -> SourceCollectionBatch:
        """Collect every configured source, leaving individual source errors as registry warnings."""
        now_utc = parse_datetime(now)
        earliest = now_utc - timedelta(hours=36)
        cutoff = max(parse_datetime(since), earliest) if since is not None else earliest
        return self.collector_registry.collect_all(self.sources, cutoff)

    def collect_and_rank_repositories(self, day: date) -> GitHubRankingBatch:
        """Rank today's snapshot before atomically persisting it, with no retention side effect."""
        collected_at = self.clock.now(_timezone(self.settings.timezone))
        baseline, has_full_baseline, history, history_warnings = (
            load_recent_snapshots_with_warnings(
                self.state_store, day, self.settings.github_window_days
            )
        )
        collection_failure: GitHubCollectionError | None = None
        try:
            names = self.discover_repositories(
                self.github_client,
                self.github_token,
                day,
                [snapshot.repository for snapshot in history],
            )
            current = self.fetch_repositories(
                self.github_client,
                self.github_token,
                names,
                collected_at,
            )
        except (httpx.HTTPError, GitHubDataError, GitHubResponseError):
            collection_failure = GitHubCollectionError(
                "GitHub data is unavailable", warnings=history_warnings
            )
        if collection_failure is not None:
            raise collection_failure

        metadata_warnings = list(history_warnings)
        if isinstance(current, GitHubSnapshotBatch):
            self._extend_unique(metadata_warnings, current.warnings)
        current_snapshots = list(current)

        retained_warnings: list[str] = []
        fallback = self._retained_snapshots(day, retained_warnings)
        self._extend_unique(metadata_warnings, retained_warnings)
        ranked = rank_repositories(
            current_snapshots,
            baseline,
            self.settings.github_top_n,
            has_full_baseline=has_full_baseline,
            fallback=fallback,
        )
        if not ranked:
            raise GitHubCollectionError(
                "GitHub ranking is unavailable",
                warnings=[*metadata_warnings, *ranked.warnings],
            )
        self.state_store.save_snapshot(day, current_snapshots)
        return GitHubRankingBatch(
            ranked,
            warnings=[*metadata_warnings, *ranked.warnings],
        )

    def write_outputs(
        self,
        local_date: str,
        rendered: RenderedDigest,
        cost: CostReport,
    ) -> OutputPaths:
        """Atomically save a durable archive and short-lived preview artifacts before send."""
        markdown = self.output_root / "digests" / f"{local_date}.md"
        html = self.output_root / "preview" / f"{local_date}.html"
        text = self.output_root / "preview" / f"{local_date}.txt"
        cost_report = self.output_root / "preview" / "cost-report.json"
        self._atomic_write(markdown, rendered.markdown)
        self._atomic_write(html, rendered.html)
        self._atomic_write(text, rendered.text)
        self._atomic_write(cost_report, self._cost_json(cost, self._usage_for_cost_report))
        return OutputPaths(markdown=markdown, html=html, text=text, cost_report=cost_report)

    def run(self, options: RunOptions) -> RunResult:
        self.delivery_outcome = "not_attempted"
        state = self.state_store.load_run_state()
        local_now = self.clock.now(_timezone(self.settings.timezone))
        local_date = local_now.date().isoformat()
        mode = options.ai_mode or self.settings.ai_mode
        result = RunResult(
            local_date=local_date,
            sent=False,
            source_total=len(self.sources),
            effective_ai_mode=mode,
            cost_currency=self.prices.currency,
        )
        self.last_result = result
        if options.send and local_date in state.sent_dates and not options.force:
            result.already_sent = True
            result.message_id = state.sent_dates[local_date]
            return result

        owned_attempt: str | None = None
        if options.send:
            owned_attempt, state = self._claim_delivery(
                state,
                local_date,
                local_now,
                options,
            )

        try:
            collection = self.collect_news(state.last_success_at, local_now)
            raw_items = collection.items
            warnings = list(collection.warnings)
            result.source_successes = collection.source_successes
            result.source_total = collection.source_total
            if result.source_total:
                result.source_success_rate = 100 * result.source_successes / result.source_total
            candidates = prepare_news(
                raw_items,
                self.settings.max_news_candidates,
                min_score=self.settings.min_news_score,
                now=local_now,
            )
            result.news_candidate_count = len(candidates)
            try:
                ranking_batch = self.collect_and_rank_repositories(local_now.date())
                ranked_repos = list(ranking_batch)
                self._extend_unique(warnings, ranking_batch.warnings)
                result.github_status = "current"
                result.github_data_date = local_date
            except GitHubCollectionError as error:
                self._extend_unique(warnings, error.warnings)
                ranked_repos, cached_day, ranking_warnings = self._load_cached_rankings(
                    local_now.date()
                )
                self._extend_unique(warnings, ranking_warnings)
                if cached_day is None:
                    warnings.append("GitHub data unavailable; no cached snapshot used")
                    result.github_status = "unavailable"
                else:
                    warnings.append(
                        "GitHub data unavailable; using cached snapshot from "
                        f"{cached_day.isoformat()}"
                    )
                    result.github_status = "cached"
                    result.github_data_date = cached_day.isoformat()

            result.warnings = list(warnings)
            if not candidates and not ranked_repos:
                raise NoUsableDigestDataError(
                    "No usable news or GitHub repository data is available"
                )

            try:
                news, repositories, usage = self.enricher.enrich(candidates, ranked_repos, mode)
            except AIEnrichmentError as error:
                warnings.append("AI enrichment failed; deterministic fallback used")
                result.effective_ai_mode = "off"
                news, repositories, fallback_usage = self.off_enricher.enrich(
                    candidates, ranked_repos, "off"
                )
                usage = [*error.usage, *fallback_usage]

            cost = calculate_cost(usage, self.prices)
            if not cost.is_complete:
                warnings.append("AI usage data incomplete; reported cost is a lower bound")
            result.warnings = warnings
            result.estimated_cost = cost.total
            result.usage = usage
            result.final_news_count = len(news)
            result.repository_count = len(repositories)
            result.ai_call_count = sum(record.call_count for record in usage)
            result.token_usage_complete = cost.is_complete
            result.cost_is_lower_bound = not cost.is_complete
            result.input_cache_hit_tokens = sum(
                record.input_cache_hit_tokens for record in usage
            )
            result.input_cache_miss_tokens = sum(
                record.input_cache_miss_tokens for record in usage
            )
            result.output_tokens = sum(record.output_tokens for record in usage)
            result.thirty_day_projection = cost.thirty_day_projection
            self._usage_for_cost_report = usage
            digest = Digest(
                local_date=local_date,
                news=news,
                repositories=repositories,
                warnings=warnings,
                usage=usage,
                estimated_cost=float(cost.total),
            )
            rendered = self.renderer(digest, cost, self.templates_dir)
            paths = self.write_outputs(local_date, rendered, cost)
            result.markdown_path = paths.markdown

            message_id: str | None = None
            if options.send:
                assert owned_attempt is not None
                with self.state_store.delivery_operation(
                    local_date, owned_attempt
                ) as delivery:
                    try:
                        delivery.mark_ambiguous()
                    except DeliveryIntentConflictError as error:
                        raise DeliveryAmbiguousError(
                            "Delivery ownership changed before the mail boundary"
                        ) from error
                    self.delivery_outcome = "ambiguous"
                    result.delivery_outcome = "ambiguous"
                    message_id = self.mailer.send(rendered)
                    self.delivery_outcome = "accepted"
                    result.delivery_outcome = "accepted"
                    result.message_id = message_id
                    try:
                        delivery.complete(message_id, local_now)
                    except DeliveryIntentConflictError as error:
                        raise DeliveryAmbiguousError(
                            "Delivery ownership changed before the accepted-state commit"
                        ) from error
                self.state_store.prune_snapshots(
                    local_now.date(), self.settings.snapshot_retention_days
                )
                result.sent = True
            return result
        except Exception:
            if owned_attempt is not None and self.delivery_outcome == "not_attempted":
                self._release_unattempted(local_date, owned_attempt)
            raise

    def _claim_delivery(
        self,
        state: RunState,
        local_date: str,
        local_now: datetime,
        options: RunOptions,
    ) -> tuple[str, RunState]:
        if options.attempt_id is not None:
            intent = state.delivery_intents.get(local_date)
            if intent is None:
                raise DeliveryReservationRequiredError(
                    "A committed delivery reservation is required"
                )
            if intent.attempt_id != options.attempt_id or intent.status != "reserved":
                raise DeliveryAmbiguousError("Delivery is blocked by unresolved intent")
            return options.attempt_id, state

        attempt_id = f"local-{uuid4().hex}"
        try:
            reservation = self.state_store.reserve_delivery(
                local_date,
                attempt_id,
                local_now,
                force=options.force,
            )
        except DeliveryIntentConflictError as error:
            raise DeliveryAmbiguousError("Delivery is blocked by unresolved intent") from error
        if reservation == "already_sent":
            raise DeliveryReservationRequiredError("Delivery date was already completed")
        return attempt_id, self.state_store.load_run_state()

    def _release_unattempted(self, local_date: str, attempt_id: str) -> None:
        try:
            self.state_store.release_delivery(local_date, attempt_id)
        except (DeliveryStateError, OSError):
            # The workflow cleanup step retries this operation. Retaining intent is conservative.
            return

    def _retained_snapshots(
        self, today: date, warnings: list[str] | None = None
    ) -> list[RepoSnapshot]:
        snapshots: list[RepoSnapshot] = []
        for snapshot_day in sorted(
            self.state_store.recent_snapshot_days(
                today,
                limit=self.settings.snapshot_retention_days,
                retention_days=self.settings.snapshot_retention_days,
            )
        ):
            rows, day_warnings = load_snapshot_safely(self.state_store, snapshot_day)
            snapshots.extend(rows)
            if warnings is not None:
                self._extend_unique(warnings, day_warnings)
        return snapshots

    def _load_cached_rankings(
        self, today: date
    ) -> tuple[list[RankedRepo], date | None, list[str]]:
        exclusion_warnings: list[str] = []
        for snapshot_day in self.state_store.recent_snapshot_days(
            today,
            limit=self.settings.snapshot_retention_days,
            retention_days=self.settings.snapshot_retention_days,
        ):
            snapshots, day_warnings = load_snapshot_safely(
                self.state_store, snapshot_day
            )
            self._extend_unique(exclusion_warnings, day_warnings)
            if snapshots:
                baseline, baseline_warnings = load_snapshot_safely(
                    self.state_store,
                    snapshot_day - timedelta(days=self.settings.github_window_days)
                )
                self._extend_unique(exclusion_warnings, baseline_warnings)
                retained_warnings: list[str] = []
                ranked = rank_repositories(
                    snapshots,
                    baseline,
                    self.settings.github_top_n,
                    has_full_baseline=bool(baseline),
                    fallback=self._retained_snapshots(snapshot_day, retained_warnings),
                )
                self._extend_unique(exclusion_warnings, retained_warnings)
                exclusion_warnings.extend(ranked.warnings)
                if ranked:
                    return ranked, snapshot_day, exclusion_warnings
        return [], None, exclusion_warnings

    @staticmethod
    def _extend_unique(target: list[str], additions: list[str] | tuple[str, ...]) -> None:
        for warning in additions:
            if warning not in target:
                target.append(warning)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _cost_json(cost: CostReport, usage: list[UsageRecord]) -> str:
        ai_call_count = sum(record.call_count for record in usage)
        known_tokens = {
            "input_cache_hit": sum(record.input_cache_hit_tokens for record in usage),
            "input_cache_miss": sum(record.input_cache_miss_tokens for record in usage),
            "output": sum(record.output_tokens for record in usage),
        }
        report = {
            "currency": cost.currency,
            "total": str(cost.total),
            "thirty_day_projection": str(cost.thirty_day_projection),
            "is_complete": cost.is_complete,
            "ai_call_count": ai_call_count,
            "token_usage_complete": cost.is_complete,
            "cost_is_lower_bound": not cost.is_complete,
            "known_tokens": known_tokens,
            "usage": [
                {
                    "stage": record.stage,
                    "model": record.model,
                    "call_count": record.call_count,
                    "input_cache_hit_tokens": record.input_cache_hit_tokens,
                    "input_cache_miss_tokens": record.input_cache_miss_tokens,
                    "output_tokens": record.output_tokens,
                    "is_complete": record.is_complete,
                }
                for record in usage
            ],
        }
        return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _timezone(name: str) -> tzinfo:
    """Resolve configured IANA names even on Windows hosts without a zoneinfo database."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        timezone = gettz(name)
        if timezone is None:
            raise
        return timezone
