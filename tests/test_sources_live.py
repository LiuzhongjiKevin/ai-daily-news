from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_daily.collectors import build_collector_registry, load_sources
from ai_daily.collectors.base import (
    PageParseError,
    SourceConfig,
    SourceParseError,
    format_source_failure,
)
from ai_daily.http import RetryingClient


def validate_collected_items(source: SourceConfig, items: list[object]) -> None:
    """Only a syntactically valid feed may be empty during live source validation."""
    if source.kind == "page" and not items:
        raise PageParseError("page returned zero valid cards")
    if source.kind in {"discovery", "github_releases"} and not items:
        raise SourceParseError(f"{source.kind} returned zero valid items")


def test_live_validation_rejects_an_empty_page_result() -> None:
    """Would catch a page selector mismatch being reported as a successful live source check."""
    source = SourceConfig(
        id="empty-page",
        name="Empty page",
        kind="page",
        source_type="official",
        url="https://example.test/news",
        language="en",
        category="company",
        item_selector="article",
        title_selector="h2",
        link_selector="a",
        date_selector="time",
        link_path_pattern=r"^/news/",
    )

    with pytest.raises(PageParseError, match="zero valid cards"):
        validate_collected_items(source, [])


@pytest.mark.parametrize("kind", ["discovery", "github_releases"])
def test_live_validation_rejects_empty_non_feed_results(kind: str) -> None:
    """Would catch an empty discovery or releases response being counted as validated."""
    source = SourceConfig(
        id=f"empty-{kind}",
        name="Empty source",
        kind=kind,
        source_type="discovery" if kind == "discovery" else "release",
        url="https://api.gdeltproject.org/api/v2/doc/doc"
        if kind == "discovery"
        else "https://api.github.com/repos/acme/widget/releases",
        language="en",
        category="company",
        allowed_domains=["example.test"] if kind == "discovery" else [],
    )

    with pytest.raises(SourceParseError, match="zero valid items"):
        validate_collected_items(source, [])


@pytest.mark.live
def test_enabled_sources_fetch_and_parse_without_collector_errors() -> None:
    """Live-only smoke test: an enabled source must return items or a valid empty parse."""
    sources = load_sources(Path("config/sources.yaml"))
    client = RetryingClient(max_attempts=1)
    registry = build_collector_registry(client)
    # This is a source-shape smoke test, not the daily collection horizon: a 90-day
    # window avoids misclassifying a valid but quiet newsroom as an empty adapter.
    since = datetime.now(UTC) - timedelta(days=90)
    failures: list[str] = []

    for source in sources:
        try:
            result = registry.collectors[source.kind].collect(source, since)
            validate_collected_items(source, result)
        except Exception as exc:  # noqa: BLE001 - one report must include every live source failure
            failures.append(f"{source.id}: {format_source_failure(source, exc)}")

    assert not failures, "enabled source validation failed: " + ", ".join(failures)
