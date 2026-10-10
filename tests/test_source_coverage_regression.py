"""Regression tests for first-party feeds and GDELT discovery quality."""
from datetime import UTC, datetime

import httpx
from pathlib import Path

from ai_daily.collectors.base import CollectorRegistry, SourceConfig, load_sources
from ai_daily.collectors.discovery import DiscoveryCollector
from ai_daily.collectors.feed import FeedCollector


class StubClient:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url, **kwargs):
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, content=value, request=httpx.Request("GET", url))


def test_official_google_sources_use_strict_feeds_and_gdelt_is_paused():
    sources = {s.id: s for s in load_sources(Path("config/sources.yaml"))}
    for source_id in ("google-deepmind", "google-ai"):
        source = sources[source_id]
        assert source.kind == "feed"
        assert source.source_type == "official"
        assert source.require_published_date
        assert source.allowed_link_hosts
    assert not any(source.kind == "discovery" for source in sources.values())


def test_strict_feed_accepts_only_dated_recent_same_publisher_items():
    source = SourceConfig(
        id="google-ai", name="Google AI", kind="feed", source_type="official",
        url="https://blog.google/rss/", language="en", category="company",
        require_published_date=True, allowed_link_hosts=["blog.google"],
    )
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>AI</title>
    <item><title>Current</title><link>https://blog.google/ai/new</link>
    <pubDate>Fri, 09 Oct 2026 08:00:00 GMT</pubDate></item>
    <item><title>Updated only</title><link>https://blog.google/ai/old</link>
    <updated>Fri, 09 Oct 2026 08:00:00 GMT</updated></item>
    <item><title>Future</title><link>https://blog.google/ai/future</link>
    <pubDate>Fri, 30 Oct 2026 08:00:00 GMT</pubDate></item>
    <item><title>Wrong host</title><link>https://attacker.example/ai</link>
    <pubDate>Fri, 09 Oct 2026 08:00:00 GMT</pubDate></item>
    <item><title>Stale</title><link>https://blog.google/ai/stale</link>
    <pubDate>Mon, 05 Oct 2026 08:00:00 GMT</pubDate></item>
    </channel></rss>"""
    now = datetime(2026, 10, 10, 8, tzinfo=UTC)
    rows = FeedCollector(StubClient({str(source.url): xml}), now=lambda: now).collect(
        source, datetime(2026, 10, 8, tzinfo=UTC)
    )
    assert [row.title for row in rows] == ["Current"]


def test_discovery_requires_independent_publication_date():
    source = SourceConfig(
        id="reuters-discovery", name="Reuters", kind="discovery",
        source_type="discovery", url="https://api.gdeltproject.org/api/v2/doc/doc",
        language="en", category="industry", allowed_domains=["reuters.com"],
    )
    payload = b"""{"articles":[
    {"title":"Old story newly indexed","url":"https://reuters.com/old",
     "seendate":"20261009T080000Z"},
    {"title":"New story dated","url":"https://reuters.com/new",
     "published_at":"2026-10-09T08:00:00Z"},
    {"title":"Untrusted","url":"https://fake-reuters.com/new",
     "published_at":"2026-10-09T08:00:00Z"}]}"""
    now = datetime(2026, 10, 10, 8, tzinfo=UTC)
    rows = DiscoveryCollector(
        StubClient({str(source.url): payload}), now=lambda: now
    ).collect(source, datetime(2026, 10, 8, tzinfo=UTC))
    assert [row.title for row in rows] == ["New story dated"]


def test_single_failed_official_feed_does_not_block_another():
    good = SourceConfig(
        id="good", name="Good", kind="feed", source_type="official",
        url="https://example.test/good.xml", language="en", category="model",
    )
    bad = good.model_copy(update={"id": "bad", "url": "https://example.test/bad.xml"})
    xml = b"""<rss version="2.0"><channel><title>Good</title><item>
    <title>Release</title><link>https://example.test/new</link>
    <pubDate>Fri, 09 Oct 2026 08:00:00 GMT</pubDate>
    </item></channel></rss>"""
    client = StubClient({
        str(good.url): xml, str(bad.url): b"<html>Denied</html>",
    })
    registry = CollectorRegistry({"feed": FeedCollector(
        client, now=lambda: datetime(2026, 10, 10, tzinfo=UTC)
    )})
    batch = registry.collect_all([bad, good], datetime(2026, 10, 8, tzinfo=UTC))
    assert batch.source_successes == 1
    assert len(batch.items) == 1
    assert len(batch.warnings) == 1
