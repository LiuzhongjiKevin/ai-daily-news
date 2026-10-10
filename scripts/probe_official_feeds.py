"""Opt-in GitHub Actions smoke probe for dated first-party Google RSS.

This script fetches only public RSS feeds, prints sanitized diagnostics, and
never sends mail, writes delivery state, or accesses production secrets.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_daily.collectors.base import format_source_failure, load_sources
from ai_daily.collectors.feed import FeedCollector
from ai_daily.http import RetryingClient

SOURCE_IDS = ("google-deepmind", "google-ai")


def main() -> int:
    sources = {source.id: source for source in load_sources(Path("config/sources.yaml"))}
    now = datetime.now(UTC)
    collector = FeedCollector(RetryingClient(), now=lambda: now)
    failures = 0
    for source_id in SOURCE_IDS:
        source = sources[source_id]
        try:
            rows = collector.collect(source, now - timedelta(days=90))
        except Exception as error:  # noqa: BLE001 - per-source smoke-test boundary
            failures += 1
            print(f"{source_id}: FAIL {format_source_failure(source, error)}")
            continue
        recent = [row for row in rows if row.published_at >= now - timedelta(hours=36)]
        print(
            f"{source_id}: PARSED "
            f"validated_90d={len(rows)} validated_36h={len(recent)}"
        )
        for row in sorted(rows, key=lambda item: item.published_at, reverse=True)[:3]:
            print(f"  {row.published_at.isoformat()} {row.canonical_url}")
        if not rows:
            failures += 1
            print(f"{source_id}: UNVERIFIED no dated articles in the 90-day window")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
