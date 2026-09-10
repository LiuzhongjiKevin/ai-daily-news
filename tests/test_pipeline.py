import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ai_daily.ai import AIEnricher, AIEnrichmentError, OffEnricher
from ai_daily.collectors import github
from ai_daily.collectors.base import SourceCollectionBatch
from ai_daily.config import AppSettings, ModelPrice, PricingTable
from ai_daily.mail import MailSendError
from ai_daily.models import NewsCluster, RankedRepo, RawItem, RepoSnapshot, RunState, UsageRecord
from ai_daily.render import RenderedDigest, render_digest
from ai_daily.state import StateStore

NOW = datetime(2026, 8, 23, 18, 30, tzinfo=UTC)
TEMPLATES = Path(__file__).parents[1] / "templates"
ATTEMPT_TIME = NOW.astimezone(timezone(timedelta(hours=8)))


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

    def collect_all(
        self, sources: list[object], since: datetime | None
    ) -> SourceCollectionBatch:
        self.since = since
        return SourceCollectionBatch(
            items=[item()],
            warnings=[],
            source_successes=len(sources),
            source_total=len(sources),
        )


class Enricher:
    def enrich(
        self, news: list[NewsCluster], repos: list[RankedRepo], mode: str
    ) -> tuple[list[NewsCluster], list[RankedRepo], list[UsageRecord]]:
        return news, repos, [UsageRecord(stage="news", model="test-model", output_tokens=10)]


class Mailer:
    def send(self, rendered: RenderedDigest) -> str:
        return "accepted-fingerprint"


class FixedRegistry(Registry):
    def __init__(
        self,
        rows: list[RawItem],
        warnings: list[str] | None = None,
        *,
        source_successes: int | None = None,
    ) -> None:
        super().__init__()
        self.rows = rows
        self.warnings = warnings or []
        self.source_successes = source_successes
        self.calls = 0

    def collect_all(
        self, sources: list[object], since: datetime | None
    ) -> SourceCollectionBatch:
        self.calls += 1
        self.since = since
        successes = (
            self.source_successes
            if self.source_successes is not None
            else max(0, len(sources) - len(self.warnings))
        )
        return SourceCollectionBatch(
            items=self.rows,
            warnings=self.warnings,
            source_successes=successes,
            source_total=len(sources),
        )


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

    def _save_run_state_unlocked(self, state: RunState) -> None:
        self.events.append("save-state")
        super()._save_run_state_unlocked(state)

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
    sources: list[object] | None = None,
    renderer: object | None = None,
) -> object:
    from ai_daily.pipeline import DailyPipeline

    return DailyPipeline(
        settings=AppSettings(ai_mode="full"),
        prices=PricingTable(
            effective_date=date(2026, 8, 24),
            models={"test-model": ModelPrice(input_cache_hit=0, input_cache_miss=0, output=1)},
        ),
        state_store=store or StateStore(tmp_path / "data"),
        sources=sources or [],  # type: ignore[arg-type]
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
        renderer=renderer or render_digest,  # type: ignore[arg-type]
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
    assert registry.since == NOW - timedelta(hours=36)
    assert result.estimated_cost == Decimal("0.000010")


@pytest.mark.parametrize('send', [False, True])
def test_tmt_only_runs_on_delivery_and_failure_does_not_block_mail(tmp_path, monkeypatch, send):
    from ai_daily.pipeline import RunOptions
    from ai_daily.translation import TmtTranslator

    calls = []
    def fail(request):
        calls.append(request)
        return httpx.Response(200, json={'Response': {'Error': {'Code': 'FailedOperation'}}})

    monkeypatch.setattr(TmtTranslator, 'from_environment', lambda: TmtTranslator(
        'fake-id', 'fake-key', client=httpx.Client(transport=httpx.MockTransport(fail))))
    pipeline = make_pipeline(tmp_path)
    result = pipeline.run(RunOptions(send=send))
    assert result.sent is send
    assert len(calls) == int(send)
    assert 'A model launch' in result.markdown_path.read_text(encoding='utf-8')


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
        "usage": [],
        "delivery_outcome": "not_attempted",
        "source_successes": 0,
        "source_total": 0,
        "source_success_rate": None,
        "news_candidate_count": 0,
        "final_news_count": 0,
        "repository_count": 0,
        "github_status": "not_run",
        "github_data_date": None,
        "effective_ai_mode": "full",
        "ai_call_count": 0,
        "token_usage_complete": True,
        "cost_is_lower_bound": False,
        "input_cache_hit_tokens": 0,
        "input_cache_miss_tokens": 0,
        "output_tokens": 0,
        "cost_currency": "USD",
        "thirty_day_projection": Decimal(0),
    }
    assert registry.calls == 0
    assert mailer.calls == 0
    assert not (tmp_path / "output").exists()
    assert result.source_total == 0
    assert result.source_success_rate is None
    assert result.github_status == "not_run"
    assert result.effective_ai_mode == "full"
    assert result.ai_call_count == 0
    assert result.delivery_outcome == "not_attempted"


def test_same_day_preview_still_generates_after_a_sent_marker(tmp_path: Path) -> None:
    """Would catch duplicate-send protection suppressing a same-day no-send preview."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_run_state(RunState(sent_dates={"2026-08-24": "prior-acceptance"}))
    registry = FixedRegistry([item()])
    mailer = RecordingMailer()

    result = make_pipeline(
        tmp_path, registry=registry, store=store, mailer=mailer
    ).run(RunOptions(send=False))

    assert result.already_sent is False
    assert result.sent is False
    assert result.message_id is None
    assert result.markdown_path and result.markdown_path.exists()
    assert registry.calls == 1
    assert mailer.calls == 0
    assert store.load_run_state().sent_dates == {"2026-08-24": "prior-acceptance"}


def test_force_resends_and_replaces_marker_only_after_new_acceptance(tmp_path: Path) -> None:
    """Would catch force being ignored or an old sent marker surviving an intentional resend."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_run_state(RunState(sent_dates={"2026-08-24": "old-acceptance"}))
    pipeline = make_pipeline(tmp_path, store=store, mailer=RecordingMailer("new-acceptance"))

    result = pipeline.run(RunOptions(send=True, force=True))

    assert result.sent is True
    assert store.load_run_state().sent_dates == {"2026-08-24": "new-acceptance"}


def test_matching_committed_reservation_owns_send_and_clears_on_acceptance(
    tmp_path: Path,
) -> None:
    """Would catch delivery bypassing or retaining the durable reservation around Graph."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)

    result = make_pipeline(tmp_path, store=store).run(
        RunOptions(send=True, attempt_id="gh-100-1")
    )

    assert result.delivery_outcome == "accepted"
    state = store.load_run_state()
    assert state.sent_dates == {"2026-08-24": "accepted-fingerprint"}
    assert state.delivery_intents == {}


def test_ambiguous_or_unowned_intent_blocks_send_even_with_force(tmp_path: Path) -> None:
    """Would catch compensation or force repeating a possibly accepted external message."""
    from ai_daily.models import DeliveryIntent
    from ai_daily.pipeline import DeliveryAmbiguousError, RunOptions

    store = StateStore(tmp_path / "data")
    store.save_run_state(
        RunState(
            delivery_intents={
                "2026-08-24": DeliveryIntent(
                    attempt_id="gh-100-1", created_at=ATTEMPT_TIME, status="ambiguous"
                )
            }
        )
    )
    registry = FixedRegistry([item()])
    mailer = RecordingMailer()

    with pytest.raises(DeliveryAmbiguousError):
        make_pipeline(tmp_path, registry=registry, store=store, mailer=mailer).run(
            RunOptions(send=True, force=True, attempt_id="gh-200-1")
        )

    assert registry.calls == 0
    assert mailer.calls == 0


def test_local_send_durably_reserves_before_mail_call(tmp_path: Path) -> None:
    """Would catch non-workflow CLI sends bypassing the same safety protocol."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")

    class InspectingMailer(RecordingMailer):
        def send(self, rendered: RenderedDigest) -> str:
            intent = store.load_run_state().delivery_intents["2026-08-24"]
            assert intent.attempt_id.startswith("local-")
            assert intent.status == "ambiguous"
            return super().send(rendered)

    result = make_pipeline(tmp_path, store=store, mailer=InspectingMailer()).run(
        RunOptions(send=True)
    )

    assert result.delivery_outcome == "accepted"
    assert store.load_run_state().delivery_intents == {}


def test_pipeline_holds_delivery_operation_across_mail_and_completion(tmp_path: Path) -> None:
    """Would catch the pipeline releasing the shared lock around the external side effect."""
    from ai_daily.pipeline import RunOptions

    class OperationTrackingStore(StateStore):
        operation_active = False

        @contextmanager
        def delivery_operation(self, local_date: str, attempt_id: str):  # type: ignore[no-untyped-def]
            with super().delivery_operation(local_date, attempt_id) as operation:
                self.operation_active = True
                try:
                    yield operation
                finally:
                    self.operation_active = False

    store = OperationTrackingStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)

    class InspectingMailer(RecordingMailer):
        def send(self, rendered: RenderedDigest) -> str:
            assert store.operation_active is True
            intent = store.load_run_state().delivery_intents["2026-08-24"]
            assert intent.status == "ambiguous"
            return super().send(rendered)

    result = make_pipeline(tmp_path, store=store, mailer=InspectingMailer()).run(
        RunOptions(send=True, attempt_id="gh-100-1")
    )

    assert result.sent is True
    assert store.operation_active is False


def test_send_reloads_and_persists_matching_owner_immediately_before_mail(
    tmp_path: Path,
) -> None:
    """Would catch a stale pipeline snapshot overwriting a newly reserved owner before Graph."""
    from ai_daily.pipeline import DeliveryAmbiguousError, RunOptions

    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)
    mailer = RecordingMailer()

    def replace_owner_before_mail(*args: object) -> RenderedDigest:
        rendered = render_digest(*args)  # type: ignore[arg-type]
        store.resolve_delivery("2026-08-24", "retry")
        store.reserve_delivery("2026-08-24", "gh-200-1", ATTEMPT_TIME)
        return rendered

    pipeline = make_pipeline(
        tmp_path,
        store=store,
        mailer=mailer,
        renderer=replace_owner_before_mail,
    )

    with pytest.raises(DeliveryAmbiguousError):
        pipeline.run(RunOptions(send=True, attempt_id="gh-100-1"))

    assert mailer.calls == 0
    intent = store.load_run_state().delivery_intents["2026-08-24"]
    assert intent.attempt_id == "gh-200-1"
    assert intent.status == "reserved"


def test_pre_mail_failure_releases_owned_reservation_as_not_attempted(tmp_path: Path) -> None:
    """Would catch deterministic generation failure needlessly blocking safe compensation."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)
    pipeline = make_pipeline(
        tmp_path,
        store=store,
        enricher=FailingEnricher(ValueError("programmer failure before mail")),
    )

    with pytest.raises(ValueError, match="programmer failure"):
        pipeline.run(RunOptions(send=True, attempt_id="gh-100-1"))

    assert pipeline.delivery_outcome == "not_attempted"
    assert store.load_run_state().delivery_intents == {}


def test_mail_failure_stays_ambiguous_until_operator_confirms_retry(tmp_path: Path) -> None:
    """Would catch compensation retrying when transport failure may hide Graph acceptance."""
    from ai_daily.pipeline import DeliveryAmbiguousError, RunOptions

    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)
    pipeline = make_pipeline(
        tmp_path, store=store, mailer=RecordingMailer(MailSendError("transport lost"))
    )

    with pytest.raises(MailSendError):
        pipeline.run(RunOptions(send=True, attempt_id="gh-100-1"))

    assert pipeline.delivery_outcome == "ambiguous"
    assert pipeline.last_result.delivery_outcome == "ambiguous"
    assert pipeline.last_result.github_status == "current"
    assert pipeline.last_result.final_news_count == 1
    assert store.load_run_state().delivery_intents["2026-08-24"].status == "ambiguous"
    with pytest.raises(DeliveryAmbiguousError):
        make_pipeline(tmp_path, store=store).run(
            RunOptions(send=True, attempt_id="gh-200-1")
        )

    store.resolve_delivery("2026-08-24", "retry")
    store.reserve_delivery("2026-08-24", "gh-200-1", ATTEMPT_TIME)
    recovered = make_pipeline(tmp_path, store=store).run(
        RunOptions(send=True, attempt_id="gh-200-1")
    )
    assert recovered.sent is True


def test_graph_acceptance_then_state_failure_remains_ambiguous_on_disk(tmp_path: Path) -> None:
    """Would catch a post-202 state error erasing evidence needed to block compensation."""
    from ai_daily.pipeline import RunOptions

    data_dir = tmp_path / "data"
    StateStore(data_dir).reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)

    class FailFinalStateStore(StateStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.saves = 0

        def _save_run_state_unlocked(self, state: RunState) -> None:
            self.saves += 1
            if self.saves == 2:
                raise OSError("simulated final state failure")
            super()._save_run_state_unlocked(state)

    store = FailFinalStateStore(data_dir)
    pipeline = make_pipeline(tmp_path, store=store)

    with pytest.raises(OSError, match="simulated final state failure"):
        pipeline.run(RunOptions(send=True, attempt_id="gh-100-1"))

    assert pipeline.delivery_outcome == "accepted"
    persisted = StateStore(data_dir).load_run_state()
    assert persisted.sent_dates == {}
    assert persisted.delivery_intents["2026-08-24"].status == "ambiguous"


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
    assert result.delivery_outcome == "not_attempted"


def test_normal_run_result_reports_metrics_from_pipeline_boundaries(tmp_path: Path) -> None:
    """Would catch daily observability being reconstructed from logs instead of typed results."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(tmp_path, sources=[object(), object()]).run(
        RunOptions(send=False, ai_mode="full")
    )

    assert result.source_successes == 2
    assert result.source_total == 2
    assert result.source_success_rate == 100.0
    assert result.news_candidate_count == 1
    assert result.final_news_count == 1
    assert result.repository_count == 1
    assert result.github_status == "current"
    assert result.github_data_date == "2026-08-24"
    assert result.effective_ai_mode == "full"
    assert result.ai_call_count == 1
    assert result.token_usage_complete is True
    assert result.cost_is_lower_bound is False
    assert result.input_cache_hit_tokens == 0
    assert result.input_cache_miss_tokens == 0
    assert result.output_tokens == 10
    assert result.cost_currency == "USD"
    assert result.estimated_cost == Decimal("0.000010")
    assert result.thirty_day_projection == Decimal("0.000300")


def test_render_failure_preserves_completed_boundary_metrics_and_releases_intent(
    tmp_path: Path,
) -> None:
    """Would catch pre-mail artifact failure erasing source, AI, cost, or warning observability."""
    from ai_daily.pipeline import RunOptions
    from ai_daily.render import render_digest

    store = StateStore(tmp_path / "data")
    store.reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)

    def fail_after_render_inputs(*args: object) -> RenderedDigest:
        render_digest(*args)  # type: ignore[arg-type]
        raise OSError("simulated artifact boundary failure")

    pipeline = make_pipeline(
        tmp_path,
        store=store,
        registry=FixedRegistry([item()], ["one-source: HTTP 503"]),
        sources=[object()],
        renderer=fail_after_render_inputs,
    )

    with pytest.raises(OSError, match="simulated artifact boundary failure"):
        pipeline.run(RunOptions(send=True, attempt_id="gh-100-1"))

    result = pipeline.last_result
    assert result.delivery_outcome == "not_attempted"
    assert result.warnings == ["one-source: HTTP 503"]
    assert result.source_successes == 0
    assert result.news_candidate_count == 1
    assert result.final_news_count == 1
    assert result.repository_count == 1
    assert result.ai_call_count == 1
    assert result.output_tokens == 10
    assert result.estimated_cost == Decimal("0.000010")
    assert store.load_run_state().delivery_intents == {}


def test_source_failure_warning_does_not_block_usable_news_and_github(tmp_path: Path) -> None:
    """Would catch one collector warning aborting otherwise usable deterministic input."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([item()], ["broken-source: HTTP 503"]),
        sources=[object(), object()],
    ).run(RunOptions())

    assert result.warnings == ["broken-source: HTTP 503"]
    assert (result.source_successes, result.source_total, result.source_success_rate) == (
        1,
        2,
        50.0,
    )


def test_source_success_metrics_use_collector_outcomes_not_warning_count(tmp_path: Path) -> None:
    """Would catch unrelated diagnostics being misclassified as failed source executions."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry(
            [item()],
            ["safe collector diagnostic"],
            source_successes=2,
        ),
        sources=[object(), object()],
    ).run(RunOptions())

    assert result.source_successes == 2
    assert result.source_total == 2
    assert result.source_success_rate == 100.0


def test_pipeline_applies_configured_news_score_threshold(tmp_path: Path) -> None:
    """Would catch low-value rule candidates bypassing the configured application threshold."""
    from ai_daily.pipeline import RunOptions

    routine = item().model_copy(
        update={
            "source_type": "media",
            "title": "Weekly AI industry roundup",
            "canonical_url": "https://media.test/roundup",
            "category": "industry",
        }
    )

    result = make_pipeline(tmp_path, registry=FixedRegistry([routine])).run(RunOptions())

    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "Weekly AI industry roundup" not in archive
    assert "今日无重大 AI 官方动态" in archive


def test_all_news_source_failures_can_still_preview_github(tmp_path: Path) -> None:
    """Would catch a total news outage suppressing a usable repository ranking."""
    from ai_daily.pipeline import RunOptions

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([], ["one: TimeoutException", "two: HTTP 503"]),
    ).run(RunOptions())

    assert result.markdown_path and "owner/repo" in result.markdown_path.read_text(encoding="utf-8")
    assert result.warnings == ["one: TimeoutException", "two: HTTP 503"]


def test_ai_enrichment_error_falls_back_but_preserves_paid_usage_and_cost(tmp_path: Path) -> None:
    """Would catch deterministic fallback replacing actual paid failure usage with zero."""
    from ai_daily.pipeline import RunOptions

    failed_usage = [
        UsageRecord(stage="news", model="test-model", call_count=2, output_tokens=10)
    ]
    failing = FailingEnricher(AIEnrichmentError("bad model JSON", usage=failed_usage))
    result = make_pipeline(tmp_path, enricher=failing, off_enricher=OffEnricher()).run(RunOptions())

    assert result.warnings == ["AI enrichment failed; deterministic fallback used"]
    assert result.estimated_cost == Decimal("0.000010")
    assert result.usage == failed_usage
    assert result.effective_ai_mode == "off"
    assert result.ai_call_count == 2
    report = json.loads((tmp_path / "output" / "preview" / "cost-report.json").read_text("utf-8"))
    assert report["usage"] == [
        {
            "stage": "news",
            "model": "test-model",
            "call_count": 2,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 0,
            "output_tokens": 10,
            "is_complete": True,
        }
    ]
    assert failing.calls == 1


@pytest.mark.parametrize("send", [False, True], ids=["preview", "send"])
@pytest.mark.parametrize("mode", ["full", "economy"])
def test_missing_ai_key_uses_zero_call_deterministic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    send: bool,
    mode: str,
) -> None:
    """Would catch no-key preview/send losing content, attempting paid AI, or leaking details."""
    from ai_daily.cli import _LazyEnricher
    from ai_daily.pipeline import RunOptions

    mailer = RecordingMailer()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = make_pipeline(
        tmp_path,
        enricher=_LazyEnricher(AppSettings(ai_model="test-model")),
        off_enricher=OffEnricher(),
        mailer=mailer,
    ).run(RunOptions(send=send, ai_mode=mode))

    assert result.sent is send
    assert mailer.calls == int(send)
    assert result.effective_ai_mode == "off"
    assert result.ai_call_count == 0
    assert result.usage == []
    assert result.estimated_cost == Decimal(0)
    assert result.warnings == ["AI enrichment failed; deterministic fallback used"]
    assert result.markdown_path is not None
    rendered = result.markdown_path.read_text("utf-8")
    assert "A model launch" in rendered
    assert "AI configuration" not in rendered


@pytest.mark.parametrize(
    ("github_usage", "expected_input_miss"),
    [
        (
            {
                "prompt_tokens": "usage-sk-secret-must-not-survive",
                "completion_tokens": 7,
            },
            17,
        ),
        (
            {
                "prompt_cache_hit_tokens": 0,
                "prompt_tokens": 23,
                "prompt_tokens_details": ["usage-sk-secret-must-not-survive"],
                "completion_tokens": 7,
            },
            40,
        ),
    ],
    ids=["malformed-token", "malformed-detail-container"],
)
def test_malformed_github_usage_falls_back_with_known_lower_bound_and_no_secret(
    tmp_path: Path, github_usage: dict[str, object], expected_input_miss: int
) -> None:
    """Would catch later malformed usage losing calls, known cost, or deterministic fallback."""
    from ai_daily.pipeline import RunOptions

    secret_usage = "usage-sk-secret-must-not-survive"

    class MalformedUsageClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs: object) -> dict[str, object]:
            messages = kwargs["messages"]
            prompt = str(messages[1]["content"])  # type: ignore[index]
            payload = json.loads(prompt.splitlines()[-1])
            if "selected_cluster_ids" in prompt:
                cluster_id = payload[0]["cluster_id"]
                content = json.dumps(
                    {
                        "selected_cluster_ids": [cluster_id],
                        "items": [
                            {
                                "cluster_id": cluster_id,
                                "summary": "模型摘要",
                                "why_it_matters": "模型价值",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                usage: dict[str, object] = {
                    "prompt_tokens": 17,
                    "completion_tokens": 5,
                }
            else:
                repository = payload[0]["repository"]
                content = json.dumps(
                    {
                        "items": [
                            {"repository": repository, "explanation": "项目说明"}
                        ]
                    },
                    ensure_ascii=False,
                )
                usage = github_usage
            return {"choices": [{"message": {"content": content}}], "usage": usage}

    enricher = AIEnricher(
        MalformedUsageClient(),
        AppSettings(ai_model="test-model", ai_max_output_tokens=256),
    )

    result = make_pipeline(
        tmp_path,
        enricher=enricher,
        off_enricher=OffEnricher(),
    ).run(RunOptions())

    assert result.warnings == [
        "AI enrichment failed; deterministic fallback used",
        "AI usage data incomplete; reported cost is a lower bound",
    ]
    assert [(record.stage, record.call_count, record.is_complete) for record in result.usage] == [
        ("news", 1, True),
        ("github", 1, False),
    ]
    assert result.usage[1].output_tokens == 7
    assert result.estimated_cost == Decimal("0.000012")
    assert result.ai_call_count == 2
    assert result.token_usage_complete is False
    assert result.cost_is_lower_bound is True
    assert result.input_cache_miss_tokens == expected_input_miss
    assert result.output_tokens == 12
    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "事件概述：" in archive
    assert "项目概览：" in archive
    assert "成本下限" in archive
    report_path = tmp_path / "output" / "preview" / "cost-report.json"
    report = json.loads(report_path.read_text("utf-8"))
    assert report["is_complete"] is False
    assert report["usage"][1]["is_complete"] is False
    for artifact in (tmp_path / "output").rglob("*"):
        if artifact.is_file():
            assert secret_usage not in artifact.read_text("utf-8")


def test_provider_transport_attempt_is_visible_in_pipeline_lower_bound(
    tmp_path: Path,
) -> None:
    """Would catch a response-loss attempt disappearing from daily call/completeness metrics."""
    from ai_daily.pipeline import RunOptions

    secret = "provider-transport-sk-secret"

    class LaterTransportFailureClient:
        def __init__(self) -> None:
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            prompt = str(kwargs["messages"][1]["content"])  # type: ignore[index]
            if "selected_cluster_ids" not in prompt:
                raise RuntimeError(secret)
            payload = json.loads(prompt.splitlines()[-1])
            cluster_id = payload[0]["cluster_id"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "selected_cluster_ids": [cluster_id],
                                    "items": [
                                        {
                                            "cluster_id": cluster_id,
                                            "summary": "模型摘要",
                                            "why_it_matters": "模型价值",
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 17, "completion_tokens": 5},
            }

    client = LaterTransportFailureClient()
    result = make_pipeline(
        tmp_path,
        enricher=AIEnricher(
            client,
            AppSettings(ai_model="test-model", ai_max_output_tokens=256),
        ),
        off_enricher=OffEnricher(),
    ).run(RunOptions())

    assert client.calls == 2
    assert [
        (record.stage, record.call_count, record.is_complete)
        for record in result.usage
    ] == [("news", 1, True), ("github", 1, False)]
    assert result.ai_call_count == 2
    assert result.token_usage_complete is False
    assert result.cost_is_lower_bound is True
    assert result.estimated_cost == Decimal("0.000005")
    assert result.warnings == [
        "AI enrichment failed; deterministic fallback used",
        "AI usage data incomplete; reported cost is a lower bound",
    ]
    for artifact in (tmp_path / "output").rglob("*"):
        if artifact.is_file():
            assert secret not in artifact.read_text("utf-8")


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
    assert result.github_status == "cached"
    assert result.github_data_date == "2026-08-23"


def test_current_github_survives_a_corrupt_historical_snapshot_with_safe_warning(
    tmp_path: Path,
) -> None:
    """Would catch one unreadable retained snapshot aborting otherwise current GitHub data."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.snapshot_dir.mkdir(parents=True)
    secret = "private-history-content"
    (store.snapshot_dir / "2026-08-23.json").write_text(secret, encoding="utf-8")

    result = make_pipeline(tmp_path, store=store).run(RunOptions(ai_mode="off"))

    assert result.github_status == "current"
    assert result.warnings == [
        "GitHub history snapshot 2026-08-23 was invalid and skipped"
    ]
    assert secret not in " ".join(result.warnings)
    assert str(tmp_path) not in " ".join(result.warnings)
    assert (store.snapshot_dir / "2026-08-24.json").exists()


def test_github_failure_skips_corrupt_newest_history_and_uses_valid_fallback(
    tmp_path: Path,
) -> None:
    """Would catch a corrupt newest cache hiding a valid older fallback or duplicating warnings."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_snapshot(date(2026, 8, 22), [snapshot()])
    (store.snapshot_dir / "2026-08-23.json").write_text("not-json", encoding="utf-8")

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    result = make_pipeline(tmp_path, store=store, discover=unavailable).run(
        RunOptions(ai_mode="off")
    )

    assert result.github_status == "cached"
    assert result.github_data_date == "2026-08-22"
    assert result.warnings == [
        "GitHub history snapshot 2026-08-23 was invalid and skipped",
        "GitHub data unavailable; using cached snapshot from 2026-08-22",
    ]


def test_all_corrupt_github_history_degrades_to_a_news_only_digest(tmp_path: Path) -> None:
    """Would catch unusable retained GitHub files blocking an otherwise valid news digest."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.snapshot_dir.mkdir(parents=True)
    secret = "corrupt-private-snapshot"
    (store.snapshot_dir / "2026-08-23.json").write_text(secret, encoding="utf-8")

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    result = make_pipeline(tmp_path, store=store, discover=unavailable).run(
        RunOptions(ai_mode="off")
    )

    assert result.github_status == "unavailable"
    assert result.repository_count == 0
    assert result.final_news_count == 1
    assert result.warnings == [
        "GitHub history snapshot 2026-08-23 was invalid and skipped",
        "GitHub data unavailable; no cached snapshot used",
    ]
    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "A model launch" in archive
    assert secret not in archive


def test_mixed_valid_and_corrupt_history_keeps_valid_trial_baseline(tmp_path: Path) -> None:
    """Would catch corrupt retention causing valid older star history to be discarded."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_snapshot(
        date(2026, 8, 20), [snapshot().model_copy(update={"stars": 10})]
    )
    (store.snapshot_dir / "2026-08-22.json").write_text("broken", encoding="utf-8")

    result = make_pipeline(tmp_path, store=store).run(RunOptions(ai_mode="off"))

    assert result.github_status == "current"
    assert result.warnings == [
        "GitHub history snapshot 2026-08-22 was invalid and skipped"
    ]
    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "7 日新增：+90" in archive


def test_malformed_github_search_data_uses_cached_snapshot(tmp_path: Path) -> None:
    """Would catch typed Search data failures escaping instead of using the existing cache path."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_snapshot(date(2026, 8, 23), [snapshot()])

    def malformed(*_: object) -> list[str]:
        raise github.GitHubDataError("GitHub search data is invalid")

    result = make_pipeline(tmp_path, store=store, discover=malformed).run(RunOptions())

    assert result.warnings == [
        "GitHub data unavailable; using cached snapshot from 2026-08-23"
    ]


def test_partial_github_metadata_warning_reaches_digest_while_usable_rows_continue(
    tmp_path: Path,
) -> None:
    """Would catch repository-level degradation being hidden from the reader and pipeline result."""
    from ai_daily.pipeline import RunOptions

    warning = "GitHub metadata partial: 1/2 repositories unavailable (data=1)"
    result = make_pipeline(
        tmp_path,
        fetch=lambda *_: github.GitHubSnapshotBatch([snapshot()], warnings=[warning]),
    ).run(RunOptions())

    assert result.warnings == [warning]
    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "GitHub metadata partial: 1/2 repositories unavailable" in archive
    assert "data=1" in archive


def test_github_ranking_exclusion_reasons_reach_the_digest_without_repository_names(
    tmp_path: Path,
) -> None:
    """Would catch mirror/anomaly filtering happening silently at the pipeline boundary."""
    from ai_daily.pipeline import RunOptions

    mirror = snapshot().model_copy(
        update={"repository": "untrusted-owner/secret-mirror", "is_mirror": True}
    )
    result = make_pipeline(
        tmp_path,
        fetch=lambda *_: github.GitHubSnapshotBatch([snapshot(), mirror]),
    ).run(RunOptions())

    assert result.warnings == ["GitHub ranking excluded 1 obvious mirror"]
    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert "GitHub ranking excluded 1 obvious mirror" in archive
    assert "untrusted-owner/secret-mirror" not in archive


def test_all_bad_github_metadata_preserves_warning_and_uses_cache(tmp_path: Path) -> None:
    """Would catch total per-repository failure losing diagnostics or skipping cached fallback."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_snapshot(date(2026, 8, 23), [snapshot()])
    warning = "GitHub metadata partial: 2/2 repositories unavailable (http_404=1, transport=1)"

    result = make_pipeline(
        tmp_path,
        store=store,
        fetch=lambda *_: github.GitHubSnapshotBatch([], warnings=[warning]),
    ).run(RunOptions())

    assert result.warnings == [
        warning,
        "GitHub data unavailable; using cached snapshot from 2026-08-23",
    ]


def test_empty_current_github_ranking_uses_older_cache_even_without_news(tmp_path: Path) -> None:
    """Would catch an empty successful GitHub fetch hiding the last usable repository snapshot."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    cached_day = date(2026, 8, 23)
    store.save_snapshot(cached_day, [snapshot()])

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([], ["all news sources unavailable"]),
        store=store,
        fetch=lambda *_: [],
    ).run(RunOptions())

    assert result.warnings == [
        "all news sources unavailable",
        "GitHub data unavailable; using cached snapshot from 2026-08-23",
    ]
    assert result.markdown_path and "owner/repo" in result.markdown_path.read_text("utf-8")
    assert not (store.snapshot_dir / "2026-08-24.json").exists()


def test_empty_current_github_ranking_prefers_cache_alongside_valid_news(tmp_path: Path) -> None:
    """Would catch a valid news section causing an empty GitHub result to bypass usable cache."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    store.save_snapshot(date(2026, 8, 23), [snapshot()])

    result = make_pipeline(tmp_path, store=store, fetch=lambda *_: []).run(RunOptions())

    assert result.warnings == ["GitHub data unavailable; using cached snapshot from 2026-08-23"]
    assert result.markdown_path and "owner/repo" in result.markdown_path.read_text("utf-8")


def test_cached_snapshot_keeps_rolling_gain_order_and_trial_baselines(tmp_path: Path) -> None:
    """Would catch cached fallback zeroing gains and sorting by total stars instead of seven-day growth."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    cached_day = date(2026, 8, 23)
    high = snapshot().model_copy(update={"repository": "high/total", "stars": 1_000})
    low = snapshot().model_copy(update={"repository": "low/growth", "stars": 200})
    trial = snapshot().model_copy(update={"repository": "trial/repo", "stars": 50})
    store.save_snapshot(cached_day, [high, low, trial])
    store.save_snapshot(
        cached_day - timedelta(days=7),
        [
            high.model_copy(update={"stars": 999}),
            low.model_copy(update={"stars": 100}),
        ],
    )
    store.save_snapshot(cached_day - timedelta(days=5), [trial.model_copy(update={"stars": 10})])

    def unavailable(*_: object) -> list[str]:
        raise httpx.ConnectError("offline")

    result = make_pipeline(
        tmp_path,
        registry=FixedRegistry([]),
        store=store,
        discover=unavailable,
    ).run(RunOptions())

    archive = result.markdown_path.read_text("utf-8") if result.markdown_path else ""
    assert archive.index("### #1 low/growth") < archive.index("### #3 high/total")
    assert "### #1 low/growth\n\n语言：未标注 · Stars：200 · 7 日新增：+100" in archive
    assert "### #2 trial/repo（试行排名）\n\n语言：未标注 · Stars：50 · 7 日新增：+40" in archive
    assert "### #3 high/total\n\n语言：未标注 · Stars：1,000 · 7 日新增：+1" in archive


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

    pipeline = make_pipeline(
        tmp_path,
        registry=FixedRegistry([], ["all sources unavailable"]),
        discover=unavailable,
        mailer=mailer,
    )
    with pytest.raises(NoUsableDigestDataError):
        pipeline.run(RunOptions(send=True))

    assert mailer.calls == 0
    assert pipeline.last_result.warnings == [
        "all sources unavailable",
        "GitHub data unavailable; no cached snapshot used",
    ]
    assert pipeline.last_result.github_status == "unavailable"
    assert not (tmp_path / "output").exists()


def test_mail_failure_leaves_marker_absent_and_intent_ambiguous(tmp_path: Path) -> None:
    """Would catch an uncertain mail request being exposed as definitely retryable."""
    from ai_daily.pipeline import RunOptions

    store = StateStore(tmp_path / "data")
    mailer = RecordingMailer(MailSendError("Graph rejected the message"))
    pipeline = make_pipeline(tmp_path, store=store, mailer=mailer)

    with pytest.raises(MailSendError):
        pipeline.run(RunOptions(send=True))

    assert store.load_run_state().sent_dates == {}
    assert store.load_run_state().delivery_intents["2026-08-24"].status == "ambiguous"


def test_state_and_pruning_follow_accepted_mail_in_exact_order(tmp_path: Path) -> None:
    """Would catch snapshot pruning or a sent marker occurring before an accepted Graph response."""
    from ai_daily.pipeline import RunOptions

    store = RecordingStore(tmp_path / "data")

    class OrderedMailer(RecordingMailer):
        def send(self, rendered: RenderedDigest) -> str:
            store.events.append("mail")
            return super().send(rendered)

    mailer = OrderedMailer()
    pipeline = make_pipeline(tmp_path, store=store, mailer=mailer)

    pipeline.run(RunOptions(send=True))

    assert store.events == [
        "load-state",
        "load-state",
        "save-state",
        "load-state",
        "save-snapshot",
        "load-state",
        "save-state",
        "mail",
        "load-state",
        "save-state",
        "prune",
    ]
    assert mailer.calls == 1


def test_pruning_does_not_run_when_state_write_fails_after_accepted_mail(tmp_path: Path) -> None:
    """Would catch retention deletion after a failed atomic sent-state write."""
    from ai_daily.pipeline import RunOptions

    class FailingStore(RecordingStore):
        def __init__(self, data_dir: Path) -> None:
            super().__init__(data_dir)
            self.saves = 0

        def _save_run_state_unlocked(self, state: RunState) -> None:
            self.events.append("save-state")
            self.saves += 1
            if self.saves == 2:
                raise OSError("disk full")
            StateStore._save_run_state_unlocked(self, state)

    data_dir = tmp_path / "data"
    StateStore(data_dir).reserve_delivery("2026-08-24", "gh-100-1", ATTEMPT_TIME)
    store = FailingStore(data_dir)
    with pytest.raises(OSError, match="disk full"):
        make_pipeline(tmp_path, store=store).run(
            RunOptions(send=True, attempt_id="gh-100-1")
        )

    assert store.events == [
        "load-state",
        "save-snapshot",
        "load-state",
        "save-state",
        "load-state",
        "save-state",
    ]


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
    assert set(report) == {
        "currency",
        "total",
        "thirty_day_projection",
        "is_complete",
        "ai_call_count",
        "token_usage_complete",
        "cost_is_lower_bound",
        "known_tokens",
        "usage",
    }
    assert report["ai_call_count"] == 1
    assert report["token_usage_complete"] is True
    assert report["cost_is_lower_bound"] is False
    assert report["known_tokens"] == {
        "input_cache_hit": 0,
        "input_cache_miss": 0,
        "output": 10,
    }
    assert set(report["usage"][0]) == {
        "stage",
        "model",
        "call_count",
        "input_cache_hit_tokens",
        "input_cache_miss_tokens",
        "output_tokens",
        "is_complete",
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


@pytest.mark.parametrize(
    ("last_success", "now", "expected"),
    [
        (None, NOW, NOW - timedelta(hours=36)),
        (NOW - timedelta(hours=48), NOW, NOW - timedelta(hours=36)),
        (NOW - timedelta(hours=5), NOW, NOW - timedelta(hours=5)),
        (
            datetime(2026, 8, 24, 1, 30, tzinfo=timezone(timedelta(hours=8))),
            NOW,
            datetime(2026, 8, 23, 17, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 23, 13, 30),  # noqa: DTZ001 - exercises naïve input normalization
            NOW,
            datetime(2026, 8, 23, 13, 30, tzinfo=UTC),
        ),
        (
            None,
            datetime(2026, 8, 23, 18, 30),  # noqa: DTZ001 - exercises naïve input normalization
            NOW - timedelta(hours=36),
        ),
    ],
)
def test_news_collection_cutoff_is_bounded_and_normalized_to_utc(
    tmp_path: Path,
    last_success: datetime | None,
    now: datetime,
    expected: datetime,
) -> None:
    """Would catch first/stale runs escaping the 36-hour window or naïve times staying naïve."""
    registry = FixedRegistry([item()])
    pipeline = make_pipeline(tmp_path, registry=registry)

    pipeline.collect_news(last_success, now)

    assert registry.since == expected
    assert registry.since.tzinfo is UTC
