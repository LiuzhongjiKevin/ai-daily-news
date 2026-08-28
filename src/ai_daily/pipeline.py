"""End-to-end orchestration and deliberate degradation for daily digests."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dateutil.tz import gettz
from pydantic import BaseModel, Field

from ai_daily.ai import AIEnrichmentError
from ai_daily.collectors.base import CollectorRegistry, SourceConfig
from ai_daily.collectors.github import discover_candidates, fetch_repo_snapshots
from ai_daily.config import AppSettings, PricingTable
from ai_daily.cost import CostReport, calculate_cost
from ai_daily.github_rank import (
    historical_candidate_names,
    load_recent_snapshots,
    rank_repositories,
)
from ai_daily.models import Digest, NewsCluster, RankedRepo, RawItem, RepoSnapshot, UsageRecord
from ai_daily.news import prepare_news
from ai_daily.render import RenderedDigest, render_digest
from ai_daily.state import StateStore


class RunOptions(BaseModel):
    send: bool = False
    force: bool = False
    ai_mode: Literal["full", "economy", "off"] | None = None


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
    """A transport failure prevents obtaining today's GitHub repository snapshot."""


class NoUsableDigestDataError(RuntimeError):
    """Neither digest section contains reader-safe data, so rendering must not continue."""


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

    def collect_news(
        self, since: datetime | None, now: datetime
    ) -> tuple[list[RawItem], list[str]]:
        """Collect every configured source, leaving individual source errors as registry warnings."""
        del now
        cutoff = since or datetime.min.replace(tzinfo=UTC)
        return self.collector_registry.collect_all(self.sources, cutoff)

    def collect_and_rank_repositories(self, day: date) -> list[RankedRepo]:
        """Rank today's snapshot before atomically persisting it, with no retention side effect."""
        collected_at = self.clock.now(_timezone(self.settings.timezone))
        try:
            names = self.discover_repositories(
                self.github_client,
                self.github_token,
                day,
                historical_candidate_names(self.state_store, day, self.settings.github_window_days),
            )
            current = self.fetch_repositories(
                self.github_client,
                self.github_token,
                names,
                collected_at,
            )
        except httpx.HTTPError as error:
            raise GitHubCollectionError("GitHub data is unavailable") from error

        baseline, has_full_baseline, _ = load_recent_snapshots(
            self.state_store, day, self.settings.github_window_days
        )
        fallback = self._retained_snapshots(day)
        ranked = rank_repositories(
            current,
            baseline,
            self.settings.github_top_n,
            has_full_baseline=has_full_baseline,
            fallback=fallback,
        )
        self.state_store.save_snapshot(day, current)
        return ranked

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
        state = self.state_store.load_run_state()
        local_now = self.clock.now(_timezone(self.settings.timezone))
        local_date = local_now.date().isoformat()
        if local_date in state.sent_dates and not options.force:
            return RunResult(
                local_date=local_date,
                sent=False,
                already_sent=True,
                message_id=state.sent_dates[local_date],
            )

        raw_items, warnings = self.collect_news(state.last_success_at, local_now)
        candidates = prepare_news(raw_items, self.settings.max_news_candidates)
        try:
            ranked_repos = self.collect_and_rank_repositories(local_now.date())
        except GitHubCollectionError:
            ranked_repos, cached_day = self._load_cached_rankings(local_now.date())
            if cached_day is None:
                warnings.append("GitHub data unavailable; no cached snapshot used")
            else:
                warnings.append(f"GitHub data unavailable; using cached snapshot from {cached_day.isoformat()}")

        if not candidates and not ranked_repos:
            raise NoUsableDigestDataError("No usable news or GitHub repository data is available")

        mode = options.ai_mode or self.settings.ai_mode
        try:
            news, repositories, usage = self.enricher.enrich(candidates, ranked_repos, mode)
        except AIEnrichmentError:
            warnings.append("AI enrichment failed; deterministic fallback used")
            news, repositories, _ = self.off_enricher.enrich(candidates, ranked_repos, "off")
            usage = []

        cost = calculate_cost(usage, self.prices)
        digest = Digest(
            local_date=local_date,
            news=news,
            repositories=repositories,
            warnings=warnings,
            usage=usage,
            estimated_cost=float(cost.total),
        )
        rendered = self.renderer(digest, cost, self.templates_dir)
        self._usage_for_cost_report = usage
        paths = self.write_outputs(local_date, rendered, cost)

        message_id = self.mailer.send(rendered) if options.send else None
        if message_id is not None:
            state.sent_dates[local_date] = message_id
            state.last_success_at = local_now
            self.state_store.save_run_state(state)
            self.state_store.prune_snapshots(local_now.date(), self.settings.snapshot_retention_days)

        return RunResult(
            local_date=local_date,
            sent=message_id is not None,
            message_id=message_id,
            markdown_path=paths.markdown,
            warnings=warnings,
            estimated_cost=cost.total,
        )

    def _retained_snapshots(self, today: date) -> list[RepoSnapshot]:
        snapshots: list[RepoSnapshot] = []
        for snapshot_day in sorted(
            self.state_store.recent_snapshot_days(
                today,
                limit=self.settings.snapshot_retention_days,
                retention_days=self.settings.snapshot_retention_days,
            )
        ):
            snapshots.extend(self.state_store.load_snapshot(snapshot_day))
        return snapshots

    def _load_cached_rankings(self, today: date) -> tuple[list[RankedRepo], date | None]:
        for snapshot_day in self.state_store.recent_snapshot_days(
            today,
            limit=self.settings.snapshot_retention_days,
            retention_days=self.settings.snapshot_retention_days,
        ):
            try:
                snapshots = self.state_store.load_snapshot(snapshot_day)
            except (OSError, ValueError):
                continue
            if snapshots:
                return rank_repositories(snapshots, [], self.settings.github_top_n), snapshot_day
        return [], None

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
        report = {
            "currency": cost.currency,
            "total": str(cost.total),
            "thirty_day_projection": str(cost.thirty_day_projection),
            "usage": [
                {
                    "stage": record.stage,
                    "model": record.model,
                    "input_cache_hit_tokens": record.input_cache_hit_tokens,
                    "input_cache_miss_tokens": record.input_cache_miss_tokens,
                    "output_tokens": record.output_tokens,
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
