from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ai_daily.collectors.base import (
    CollectorRegistry,
    FeedParseError,
    PageParseError,
    SourceConfig,
    SourceConfigurationError,
    SourceParseError,
    format_collection_error,
    format_source_failure,
)
from ai_daily.collectors.discovery import DiscoveryCollector
from ai_daily.collectors.feed import FeedCollector
from ai_daily.collectors.github import GitHubReleaseCollector
from ai_daily.collectors.page import PageCollector

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureClient:
    """Small transport double that keeps collector parsing fully real."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[object, object]]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append((url, kwargs))
        if url not in self.responses:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("missing fixture", request=request, response=httpx.Response(404))
        return httpx.Response(200, content=self.responses[url], request=httpx.Request("GET", url))


@pytest.mark.parametrize(
    "document",
    [
        "{}\n",
        "sources: not-a-list\n",
        "sources:\n  - id: incomplete\n",
    ],
)
def test_load_sources_normalizes_malformed_yaml_to_safe_configuration_error(
    tmp_path: Path, document: str
) -> None:
    """Would catch top-level or entry validation failures escaping as parser tracebacks."""
    path = tmp_path / "sources.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(SourceConfigurationError, match="Source configuration is invalid") as error:
        from ai_daily.collectors.base import load_sources

        load_sources(path)

    assert "incomplete" not in str(error.value)


@pytest.fixture
def http_client() -> FixtureClient:
    return FixtureClient(
        {
            "https://example.test/feed.xml": (FIXTURES / "feed.xml").read_bytes(),
            "https://example.test/news": (FIXTURES / "news_page.html").read_bytes(),
            "https://api.github.com/repos/acme/widget/releases": (
                FIXTURES / "github_releases.json"
            ).read_bytes(),
            "https://api.gdeltproject.org/api/v2/doc/doc": (FIXTURES / "discovery.json").read_bytes(),
        }
    )


def test_feed_collector_maps_entries_and_discards_old_items(http_client: FixtureClient) -> None:
    """Would catch a collector that leaks stale feed entries or maps the wrong fields."""
    source = SourceConfig(
        id="deepmind",
        name="Google DeepMind",
        kind="feed",
        source_type="official",
        url="https://example.test/feed.xml",
        language="en",
        category="model",
    )

    items = FeedCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))

    assert [item.title for item in items] == ["A new model release"]
    assert items[0].source_type == "official"
    assert str(items[0].canonical_url) == "https://example.test/posts/model"
    assert items[0].excerpt == "A concise announcement."
    assert items[0].published_at == datetime(2026, 8, 24, 8, 30, tzinfo=UTC)


def test_feed_collector_rejects_malformed_content_that_has_no_entries(
    http_client: FixtureClient,
) -> None:
    """Would catch an HTML denial page being falsely accepted as a valid empty feed."""
    source = SourceConfig(
        id="broken-feed",
        name="Broken feed",
        kind="feed",
        source_type="official",
        url="https://example.test/broken-feed.xml",
        language="en",
        category="model",
    )
    http_client.responses[str(source.url)] = b"<html><title>Access denied</title></html>"

    with pytest.raises(FeedParseError, match="invalid feed"):
        FeedCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))


def test_feed_collector_accepts_a_valid_empty_feed(http_client: FixtureClient) -> None:
    """Would catch valid empty publisher feeds being misreported as malformed transports."""
    source = SourceConfig(
        id="empty-feed",
        name="Empty feed",
        kind="feed",
        source_type="official",
        url="https://example.test/empty-feed.xml",
        language="en",
        category="model",
    )
    http_client.responses[str(source.url)] = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'
    )

    assert FeedCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC)) == []


def test_page_collector_uses_configured_selectors_and_resolves_relative_links(
    http_client: FixtureClient,
) -> None:
    """Would catch a selector-based page collector that ignores configured selectors or URL bases."""
    source = SourceConfig(
        id="newsroom",
        name="Example Newsroom",
        kind="page",
        source_type="official",
        url="https://example.test/news",
        language="en",
        category="company",
        item_selector="article.story",
        title_selector="h2",
        link_selector="a.read-more",
        date_selector="time",
        excerpt_selector="p.summary",
        link_path_pattern=r"^/releases/",
    )

    items = PageCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))

    assert [item.title for item in items] == ["Page announcement"]
    assert str(items[0].canonical_url) == "https://example.test/releases/page-announcement"
    assert items[0].excerpt == "Details without markup."
    assert items[0].published_at == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def test_page_source_config_requires_a_link_path_contract() -> None:
    """Would catch a broad page selector being configured without a source-specific URL boundary."""
    with pytest.raises(ValueError, match="link_path_pattern"):
        SourceConfig(
            id="unbounded-page",
            name="Unbounded page",
            kind="page",
            source_type="official",
            url="https://example.test/news",
            language="en",
            category="company",
            item_selector="article",
            title_selector="h2",
            link_selector="a",
            date_selector="time",
        )


def test_page_collector_reports_a_selector_mismatch_instead_of_returning_empty(
    http_client: FixtureClient,
) -> None:
    """Would catch a changed page card shape being silently treated as no news."""
    source = SourceConfig(
        id="newsroom",
        name="Example Newsroom",
        kind="page",
        source_type="official",
        url="https://example.test/news",
        language="en",
        category="company",
        item_selector="article.story",
        title_selector="h2",
        link_selector="a.read-more",
        date_selector="time.missing",
        excerpt_selector="p.summary",
        link_path_pattern=r"^/releases/",
    )

    with pytest.raises(PageParseError, match="matched 2 cards but parsed 0"):
        PageCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))


def test_page_collector_reports_invalid_card_urls_as_a_source_shape_error(
    http_client: FixtureClient,
) -> None:
    """Would catch invalid page links escaping as opaque Pydantic validation errors."""
    source = SourceConfig(
        id="newsroom",
        name="Example Newsroom",
        kind="page",
        source_type="official",
        url="https://example.test/invalid-card",
        language="en",
        category="company",
        item_selector="article",
        title_selector="h2",
        link_selector="a",
        date_selector="time",
        link_path_pattern=r"^/releases/",
    )
    http_client.responses[str(source.url)] = (
        b"<article><h2>Bad card</h2><a href='mailto:news@example.test'>Read</a>"
        b"<time datetime='2026-08-24T09:00:00Z'></time></article>"
    )

    with pytest.raises(PageParseError, match="matched 1 cards but parsed 0"):
        PageCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))


def test_page_collector_rejects_incidental_cards_outside_the_source_news_path(
    http_client: FixtureClient,
) -> None:
    """Would catch unrelated article cards being accepted merely because broad selectors match."""
    source = SourceConfig(
        id="newsroom",
        name="Example Newsroom",
        kind="page",
        source_type="official",
        url="https://example.test/news",
        language="en",
        category="company",
        item_selector="article.story",
        title_selector="h2",
        link_selector="a.read-more",
        date_selector="time",
        link_path_pattern=r"^/official-news/",
    )

    with pytest.raises(PageParseError, match="matched 2 cards but parsed 0"):
        PageCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))


def test_github_release_collector_maps_api_releases(http_client: FixtureClient) -> None:
    """Would catch releases being read from the wrong JSON fields or old releases being retained."""
    source = SourceConfig(
        id="widget",
        name="Acme Widget",
        kind="github_releases",
        source_type="release",
        url="https://api.github.com/repos/acme/widget/releases",
        language="en",
        category="open-source",
    )

    items = GitHubReleaseCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))

    assert [item.title for item in items] == ["v2.0.0"]
    assert str(items[0].canonical_url) == "https://github.com/acme/widget/releases/tag/v2.0.0"
    assert items[0].excerpt == "Major improvements."
    assert items[0].published_at == datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def test_discovery_collector_filters_every_result_not_in_the_domain_allowlist(
    http_client: FixtureClient,
) -> None:
    """Would catch GDELT results from untrusted domains entering the digest."""
    source = SourceConfig(
        id="reuters-discovery",
        name="Reuters AI",
        kind="discovery",
        source_type="discovery",
        url="https://api.gdeltproject.org/api/v2/doc/doc",
        language="en",
        category="industry",
        allowed_domains=["reuters.com"],
    )

    items = DiscoveryCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))

    assert [item.title for item in items] == ["Allowed AI report"]
    assert str(items[0].canonical_url) == "https://www.reuters.com/technology/allowed-ai-report"
    assert items[0].source_name == "Reuters AI"
    assert http_client.requests[-1][1]["params"]["format"] == "json"  # type: ignore[index]


def test_discovery_collector_reports_non_json_transport_without_a_response_body(
    http_client: FixtureClient,
) -> None:
    """Would catch an HTML error page being reduced to an unactionable JSON decoder class."""
    source = SourceConfig(
        id="reuters-discovery",
        name="Reuters AI",
        kind="discovery",
        source_type="discovery",
        url="https://api.gdeltproject.org/api/v2/doc/doc",
        language="en",
        category="industry",
        allowed_domains=["reuters.com"],
    )
    http_client.responses[str(source.url)] = b"<html><title>Rate limited</title></html>"

    with pytest.raises(SourceParseError, match="invalid discovery JSON content-type=unknown"):
        DiscoveryCollector(http_client).collect(source, datetime(2026, 8, 23, tzinfo=UTC))


def test_registry_isolates_one_source_failure_while_returning_other_source_items(
    http_client: FixtureClient,
) -> None:
    """Would catch a single failed source preventing the scheduled collection from completing."""
    valid = SourceConfig(
        id="deepmind",
        name="Google DeepMind",
        kind="feed",
        source_type="official",
        url="https://example.test/feed.xml",
        language="en",
        category="model",
    )
    failing = valid.model_copy(update={"id": "unavailable", "url": "https://example.test/missing.xml"})
    registry = CollectorRegistry({"feed": FeedCollector(http_client)})

    items, warnings = registry.collect_all([valid, failing], datetime(2026, 8, 23, tzinfo=UTC))

    assert [item.source_id for item in items] == ["deepmind"]
    assert warnings == [
        "unavailable: HTTP 404 content-type=unknown url=https://example.test/missing.xml"
    ]


def test_registry_warning_contains_redacted_actionable_http_diagnostics() -> None:
    """Would catch source warnings that hide status, media type, or leak request query secrets."""
    request = httpx.Request("GET", "https://example.test/feed.xml?token=secret")
    response = httpx.Response(429, headers={"content-type": "text/html"}, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)

    diagnostic = format_collection_error(error)

    assert "HTTP 429" in diagnostic
    assert "content-type=text/html" in diagnostic
    assert "https://example.test/feed.xml?<redacted>" in diagnostic
    assert "secret" not in diagnostic


def test_registry_adds_a_redacted_endpoint_to_page_parse_diagnostics(
    http_client: FixtureClient,
) -> None:
    """Would catch an actionable parser error being detached from the failing source endpoint."""
    source = SourceConfig(
        id="bad-page",
        name="Bad page",
        kind="page",
        source_type="official",
        url="https://example.test/news?token=secret",
        language="en",
        category="company",
        item_selector=".missing",
        title_selector="h2",
        link_selector="a",
        date_selector="time",
        link_path_pattern=r"^/news/",
    )
    http_client.responses[str(source.url)] = (FIXTURES / "news_page.html").read_bytes()
    registry = CollectorRegistry({"page": PageCollector(http_client)})

    _, warnings = registry.collect_all([source], datetime(2026, 8, 23, tzinfo=UTC))

    assert warnings == [
        (
            "bad-page: PageParseError: page selector mismatch: '.missing' matched 0 cards "
            "url=https://example.test/news?<redacted>"
        )
    ]
    assert "secret" not in warnings[0]


def test_format_source_failure_includes_a_redacted_configured_endpoint() -> None:
    """Would catch non-HTTP failures losing the endpoint needed to repair their configuration."""
    source = SourceConfig(
        id="bad-page",
        name="Bad page",
        kind="page",
        source_type="official",
        url="https://example.test/news?token=secret",
        language="en",
        category="company",
        item_selector="article",
        title_selector="h2",
        link_selector="a",
        date_selector="time",
        link_path_pattern=r"^/news/",
    )

    diagnostic = format_source_failure(source, PageParseError("page returned zero valid cards"))

    assert diagnostic.endswith("url=https://example.test/news?<redacted>")
    assert "secret" not in diagnostic


def test_load_sources_excludes_disabled_entries(tmp_path: Path) -> None:
    """Would catch disabled sources being fetched despite their explicit configuration."""
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - id: enabled\n"
        "    name: Enabled\n"
        "    kind: feed\n"
        "    source_type: official\n"
        "    url: https://example.test/feed.xml\n"
        "    language: en\n"
        "    category: model\n"
        "  - id: disabled\n"
        "    name: Disabled\n"
        "    kind: feed\n"
        "    source_type: official\n"
        "    url: https://example.test/off.xml\n"
        "    language: en\n"
        "    category: model\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    from ai_daily.collectors.base import load_sources

    assert [source.id for source in load_sources(path)] == ["enabled"]


def test_configured_registry_covers_required_sources_with_unique_ids() -> None:
    """Would catch a required publisher disappearing or a duplicate source overriding another one."""
    from ai_daily.collectors.base import load_sources

    sources = load_sources(Path("config/sources.yaml"))
    expected_ids = {
        "kimi",
        "glm",
        "deepseek",
        "qwen",
        "doubao",
        "wenxin",
        "hunyuan",
        "minimax",
        "stepfun",
        "baichuan",
        "zero-one-ai",
        "sparkdesk",
        "sensetime",
        "modelscope",
        "openai",
        "anthropic",
        "google-deepmind",
        "google-ai",
        "meta-ai",
        "microsoft-ai",
        "xai",
        "mistral",
        "cohere",
        "perplexity",
        "amazon-ai",
        "apple-ml",
        "nvidia-ai",
        "amd-ai",
        "intel-ai",
        "hugging-face",
        "databricks-ai",
        "runway",
        "stability-ai",
        "elevenlabs",
        "github-blog",
        "github-changelog",
        "techcrunch-ai",
        "the-verge-ai",
        "ars-technica",
        "venturebeat-ai",
        "mit-tech-review",
        "hacker-news",
        "reuters-discovery",
        "jiqizhixin-discovery",
        "qbitai-discovery",
        "36kr-discovery",
        "aiera-discovery",
        "ithome-discovery",
        "cls-discovery",
        "huxiu-discovery",
        "tmtpost-discovery",
    }

    assert expected_ids <= {source.id for source in sources}
    assert len({source.id for source in sources}) == len(sources)
    assert all(source.source_type and source.category for source in sources)
    page_sources = [source for source in sources if source.kind == "page"]
    assert all(source.link_path_pattern and source.link_path_pattern.startswith("^/") for source in page_sources)
