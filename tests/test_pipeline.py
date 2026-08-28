import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ai_daily.ai import AIEnrichmentError
from ai_daily.config import AppSettings, ModelPrice, PricingTable
from ai_daily.mail import MailSendError
from ai_daily.models import NewsCluster, RankedRepo, RawItem, RepoSnapshot, RunState, UsageRecord
from ai_daily.render import RenderedDigest
from ai_daily.state import StateStore

NOW = datetime(2026, 8, 23, 18, 30, tzinfo=UTC)
TEMPLATES = Path(__file__).parents[1] / "templates"


def item() -> RawItem:
    return RawItem(
        source_id="official",
        source_name="Official",
        source_type="official",
        title="A model launch",
        published_at=NOW,
        canonical_url="https://example.test/launch",
        language="en",
        category="model",
    )


def snapshot() -> RepoSnapshot:
    return RepoSnapshot(
        repository="owner/repo",
        stars=100,
        forks=2,
        updated_at=NOW,
        collected_at=NOW,
    )


class Clock:
    def now(self, timezone: object) -> datetime:
        return NOW.astimezone(timezone)  # type: ignore[arg-type]


class Registry:
    def __init__(self) -> None:
        self.since: datetime | None = None

    def collect_all(self, sources: list[object], since: datetime | None) -> tuple[list[RawItem], list[str]]:
        self.since = since
        return [item()], []


class Enricher:
    def enrich(
        self, news: list[NewsCluster], repos: list[RankedRepo], mode: str
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]:
        return news, repos, [UsageRecord(stage="news", model="test-model", output_tokens=10)]


class Mailer:
    def send(self, rendered: RenderedDigest) -> str:
        return "accepted-fingerprint"


class FixedRegistry(Registry):
    def __init__(self, rows: list[RawItem], warnings: list[str] | None = None) -> None:
        super().__init__()
        self.rows = rows
        self.warnings = warnings or []
        self.calls = 0

    def collect_all(self, sources: list[object], since: datetime | None) -> tuple[list[RawItem], list[str]]:
        self.calls += 1
        self.since = since
        return self.rows, self.warnings


class FailingEnricher:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def enrich(
        self, news: list[NewsCluster], repos: list[RankedRepo], mode: str
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]:
        self.calls += 1
        raise self.error


class RecordingMailer:
    def __init__(self, response: str | Exception = "accepted-fingerprint") -> None:
        self.response = response
        self.calls = 0

    def send(self, rendered: RenderedDigest) -> str:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RecordingStore(StateStore):
    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.events: list[str] = []

    def load_run_state(self) -> RunState:
        self.events.append("load-state")
        return super().load_run_state()

    def save_snapshot(self, day: date, snapshots: list[RepoSnapshot]) -> Path:
        self.events.append("save-snapshot")
        return super().save_snapshot(day, snapshots)

    def save_run_state(self, state: RunState) -> None:
        self.events.append("save-state")
        super().save_run_state(state)

    def prune_snapshots(self, today: date, retention_days: int) -> list[Path]:
        self.events.append("prune")
        return super().prune_snapshots(today, retention_days)


def make_pipeline(
    tmp_path: Path,
    *,
    registry: FixedRegistry | None = None,
    store: StateStore | None = None,
    enricher: object | None = None,
    off_enricher: object | None = None,
    mailer: object | None = None,
    discover: object | None = None,
    fetch: object | None = None,
) -> object:
    from ai_daily.pipeline import DailyPipeline

    return DailyPipeline(
        settings=AppSettings(ai_mode="full"),
        prices=PricingTable(
            effective_date=date(2026, 8, 24),
            models={"test-model": ModelPrice(input_cache_hit=0, input_cache_miss=0, output=1)},
        ),
        state_store=store or StateStore(tmp_path / "data"),
        sources=[],
        collector_registry=registry or FixedRegistry([item()]),
        github_client=object(),
        github_token="test-token",
        enricher=enricher or Enricher(),  # type: ignore[arg-type]
        off_enricher=off_enricher or Enricher(),  # type: ignore[arg-type]
        mailer=mailer or RecordingMailer(),  # type: ignore[arg-type]
        templates_dir=TEMPLATES,
        output_root=tmp_path / "output",
        clock=Clock(),
        discover_repositories=discover or (lambda *_: ["owner/repo"]),  # type: ignore[arg-type]
        fetch_repositories=fetch or (lambda *_: [snapshot()]),  # type: ignore[arg-type]
    )


def test_pipeline_generates_archives_sends_and_marks_beijing_date(tmp_path: Path) -> None:
    """Would catch orchestration that sends before persisting rendered daily artifacts."""
    from ai_daily.pipeline import DailyPipeline, RunOptions

    settings = AppSettings(ai_mode="off")
    prices = PricingTable(
        effective_date=date(2026, 8, 24),
        models={"test-model": ModelPrice(input_cache_hit=0, input_cache_miss=0, output=1)},
    )
    registry = Registry()
    store = StateStore(tmp_path / "data")

    pipeline = DailyPipeline(
        settings=settings,
        prices=prices,
        state_store=store,
        sources=[],
        collector_registry=registry,
        github_client=object(),
        github_token="test-token",
        enricher=Enricher(),
        off_enricher=Enricher(),
        mailer=Mailer(),
        templates_dir=TEMPLATES,
        output_root=tmp_path / "output",
        clock=Clock(),
        discover_repositories=lambda *_: ["owner/repo"],
        fetch_repositories=lambda *_: [snapshot()],
    )

    result = pipeline.run(RunOptions(send=True))

    assert result.local_date == "2026-08-24"
    assert result.sent is True
    assert result.markdown_path == tmp_path / "output" / "digests" / "2026-08-24.md"
    assert store.load_run_state().sent_dates["2026-08-24"] == "accepted-fingerprint"
    assert registry.since == datetime.min.replace(tzinfo=UTC)
    assert result.estimated_cost == Decimal("0.000010")


def test_already_sent_date_is_a_true_no_op(tmp_path: Path) -> None:
    """Would catch duplicate protection doing work or resending after a prior accepted Graph result."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_run_state(RunState(sent_dates={"2026-08-24": "prior-acceptance"}))
    registry = FixedRegistry([item()])
    mailer = RecordingMailer()
    pipeline = make_pipeline(tmp_path, registry=registry, store=store, mailer=mailer)

    result = pipeline.run(RunOptions(send=True))

    assert result.model_dump() == {
        "local_date": "2026-08-24",
        "sent": False,
        "already_sent": True,
        "message_id": "prior-acceptance",
        "markdown_path": None,
        "warnings": [],
        "estimated_cost": Decimal(0),
    }
    assert registry.calls == 0
    assert mailer.calls == 0
    assert not (tmp_path / "output").exists()


def test_force_resends_and_replaces_marker_only_after_new_acceptance(tmp_path: Path) -> None:
    """Would catch force being ignored or an old sent marker surviving an intentional resend."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_run_state(RunState(sent_dates={"2026-08-24": "old-acceptance"}))
    pipeline = make_pipeline(tmp_path, store=store, mailer=RecordingMailer("new-acceptance"))

    result = pipeline.run(RunOptions(send=True, force=True))

    assert result.sent is True
    assert store.load_run_state().sent_dates == {"2026-08-24": "new-acceptance"}


def test_preview_writes_all_artifacts_without_creating_a_sent_marker(tmp_path: Path) -> None:
    """Would catch a preview run being mistaken for a delivered digest."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    mailer = RecordingMailer()
    result = make_pipeline(tmp_path, store=store, mailer=mailer).run(RunOptions(send=False))

    assert result.sent is False
    assert result.markdown_path and result.markdown_path.exists()
    assert (tmp_path / "output" / "preview" / "2026-08-24.html").exists()
    assert (tmp_path / "output" / "preview" / "2026-08-24.txt").exists()
    assert (tmp_path / "output" / "preview" / "cost-report.json").exists()
    assert store.load_run_state() == RunState()
    assert mailer.calls == 0


def test_source_failure_warning_does_not_block_usable_news_and_github(tmp_path: Path) -> None:
    """Would catch one collector warning aborting otherwise usable deterministic input."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([item()], ["broken-source: HTTP 503"]),
    ).run(RunOptions())

    assert result.warnings == ["broken-source: HTTP 503"]


def test_all_news_source_failures_can_still_preview_github(tmp_path: Path) -> None:
    """Would catch a total news outage suppressing a usable repository ranking."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([], ["one: TimeoutException", "two: HTTP 503"]),
    ).run(RunOptions())

    assert result.markdown_path and "owner/repo" in result.markdown_path.read_text(encoding="utf-8")
    assert result.warnings == ["one: TimeoutException", "two: HTTP 503"]


def test_ai_enrichment_error_uses_exact_off_mode_warning_and_zero_usage(tmp_path: Path) -> None:
    """Would catch a model failure becoming fatal or retaining paid usage from failed enrichment."""
    from ai_daily.pipeline import RunOptions

    failing = FailingEnricher(AIEnrichmentError("bad model JSON"))
    result = make_pipeline(tmp_path, enricher=failing, off_enricher=Enricher()).run(RunOptions())

    assert result.warnings == ["AI enrichment failed; deterministic fallback used"]
    assert result.estimated_cost == Decimal("0.000000")
    report = json.loads((tmp_path / "output" / "preview" / "cost-report.json").read_text("utf-8"))
    assert report["usage"] == []
    assert failing.calls == 1


def test_github_transport_failure_uses_newest_cached_snapshot_and_exposes_date(tmp_path: Path) -> None:
    """Would catch degraded GitHub output silently dropping its snapshot date or using an older cache."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    newest = date(2026, 8, 23)
    store.save_snapshot(date(2026, 8, 20), [snapshot().model_copy(update={"stars": 10})])
    store.save_snapshot(newest, [snapshot()])

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    result = make_pipeline(tmp_path, store=store, discover=unavailable).run(RunOptions())

    assert result.warnings == ["GitHub data unavailable; using cached snapshot from 2026-08-23"]
    assert result.markdown_path and "owner/repo" in result.markdown_path.read_text("utf-8")
    assert "2026-08-23" in result.markdown_path.read_text("utf-8")


def test_github_failure_without_cache_keeps_valid_news_and_warns(tmp_path: Path) -> None:
    """Would catch an unavailable optional repository section blocking a valid news digest."""
    from ai_daily.pipeline import RunOptions

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    result = make_pipeline(tmp_path, discover=unavailable).run(RunOptions())

    assert result.warnings == ["GitHub data unavailable; no cached snapshot used"]
    assert result.markdown_path and "## GitHub" in result.markdown_path.read_text("utf-8")


def test_no_usable_sections_fails_before_render_or_send(tmp_path: Path) -> None:
    """Would catch a misleading empty digest being rendered when both data sections are unavailable."""
    from ai_daily.pipeline import NoUsableDigestDataError, RunOptions

    mailer = RecordingMailer()

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    with pytest.raises(NoUsableDigestDataError):
        make_pipeline(
            tmp_path,
            registry=FixedRegistry([], ["all sources unavailable"]),
            discover=unavailable,
            mailer=mailer,
        ).run(RunOptions(send=True))

    assert mailer.calls == 0
    assert not (tmp_path / "output").exists()


def test_mail_failure_leaves_marker_absent_for_later_successful_compensation(tmp_path: Path) -> None:
    """Would catch a failed mail request consuming the date-based retry opportunity."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    mailer = RecordingMailer(MailSendError("Graph rejected the message"))
    pipeline = make_pipeline(tmp_path, store=store, mailer=mailer)

    with pytest.raises(MailSendError):
        pipeline.run(RunOptions(send=True))

    assert store.load_run_state().sent_dates == {}
    mailer.response = "accepted-on-compensation"
    result = pipeline.run(RunOptions(send=True))
    assert result.sent is True
    assert store.load_run_state().sent_dates == {"2026-08-24": "accepted-on-compensation"}


def test_state_and_pruning_follow_accepted_mail_in_exact_order(tmp_path: Path) -> None:
    """Would catch snapshot pruning or a sent marker occurring before an accepted Graph response."""
    from ai_daily.pipeline import RunOptions

    store = RecordingStore(tmp_path / "data")
    mailer = RecordingMailer()
    pipeline = make_pipeline(tmp_path, store=store, mailer=mailer)

    pipeline.run(RunOptions(send=True))

    assert store.events == ["load-state", "save-snapshot", "save-state", "prune"]
    assert mailer.calls == 1


def test_pruning_does_not_run_when_state_write_fails_after_accepted_mail(tmp_path: Path) -> None:
    """Would catch retention deletion after a failed atomic sent-state write."""
    from ai_daily.pipeline import RunOptions

    class FailingStore(RecordingStore):
        def save_run_state(self, state: RunState) -> None:
            self.events.append("save-state")
            raise OSError("disk full")

    store = FailingStore(tmp_path / "data")
    with pytest.raises(OSError, match="disk full"):
        make_pipeline(tmp_path, store=store).run(RunOptions(send=True))

    assert store.events == ["load-state", "save-snapshot", "save-state"]


def test_output_paths_are_atomic_and_cost_report_contains_only_safe_allowlisted_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Would catch partial artifacts or report serialization of prompts, content, endpoints, or addresses."""
    from ai_daily.pipeline import RunOptions

    replacements: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def record_replace(source: Path, target: Path) -> Path:
        replacements.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", record_replace)
    result = make_pipeline(tmp_path).run(RunOptions())
    preview = tmp_path / "output" / "preview"
    report_path = preview / "cost-report.json"
    report = json.loads(report_path.read_text("utf-8"))

    assert result.markdown_path == tmp_path / "output" / "digests" / "2026-08-24.md"
    assert [target for _, target in replacements] == [
        tmp_path / "data" / "github" / "2026-08-24.json",
        tmp_path / "output" / "digests" / "2026-08-24.md",
        preview / "2026-08-24.html",
        preview / "2026-08-24.txt",
        report_path,
    ]
    assert not list((tmp_path / "output").rglob("*.tmp"))
    assert set(report) == {"currency", "total", "thirty_day_projection", "usage"}
    assert set(report["usage"][0]) == {
        "stage",
        "model",
        "input_cache_hit_tokens",
        "input_cache_miss_tokens",
        "output_tokens",
    }
    assert "test-token" not in report_path.read_text("utf-8")
    assert "example.test" not in report_path.read_text("utf-8")


def test_unexpected_exceptions_are_not_silently_downgraded(tmp_path: Path) -> None:
    """Would catch programmer/configuration failures being mislabeled as AI or GitHub degradation."""
    from ai_daily.pipeline import RunOptions

    with pytest.raises(ValueError, match="bad configuration"):
        make_pipeline(
            tmp_path,
            enricher=FailingEnricher(ValueError("bad configuration")),
        ).run(RunOptions())

    def programming_error(*_: object) -> list[str]:
        raise RuntimeError("unexpected invariant failure")

    with pytest.raises(RuntimeError, match="unexpected invariant"):
        make_pipeline(tmp_path / "github", discover=programming_error).run(RunOptions())


def test_last_success_time_is_the_next_collection_cutoff(tmp_path: Path) -> None:
    """Would catch the pipeline using local date boundaries instead of the persisted success instant."""
    from ai_daily.pipeline import RunOptions

    previous = NOW - timedelta(hours=5)
    store = StateStore(tmp_path / "data")
    store.save_run_state(RunState(last_success_at=previous))
    registry = FixedRegistry([item()])

    make_pipeline(tmp_path, registry=registry, store=store).run(RunOptions())

    assert registry.since == previous
