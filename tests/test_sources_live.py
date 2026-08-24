from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_daily.collectors import build_collector_registry, load_sources
from ai_daily.http import RetryingClient


@pytest.mark.live
def test_enabled_sources_fetch_and_parse_without_collector_errors() -> None:
    """Live-only smoke test: an enabled source must return items or a valid empty parse."""
    sources = load_sources(Path("config/sources.yaml"))
    client = RetryingClient(max_attempts=1)
    registry = build_collector_registry(client)
    since = datetime.now(UTC) - timedelta(days=2)
    failures: list[str] = []

    for source in sources:
        try:
            result = registry.collectors[source.kind].collect(source, since)
            assert isinstance(result, list)
        except Exception as exc:  # noqa: BLE001 - one report must include every live source failure
            failures.append(f"{source.id}: {type(exc).__name__}")

    assert not failures, "enabled source validation failed: " + ", ".join(failures)
