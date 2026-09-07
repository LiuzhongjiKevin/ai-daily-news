from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ai_daily.collectors.base import PageParseError, load_sources
from ai_daily.collectors.page import PageCollector

SOURCES = {s.id: s for s in load_sources(Path(__file__).parents[1] / "config/sources.yaml")}
CASES = [
    ("kimi-blog", '<div><a href="/en/blog/model"></a><div><h4>Model</h4>'
     '<p class="card-date">2026-08-26</p></div></div>'),
    ("qwen", '<article class="post-entry"><h2>Model</h2><footer class="entry-footer">'
     '<span>August 26, 2026</span></footer><a class="entry-link" href="/blog/model/">'
     '</a></article>'),
    ("glm-release-notes", '<div class="update-container" id="2026-08-26">'
     '<button>2026-08-26</button><p>Model</p><p>Details</p></div>'),
    ("deepseek", '<article><h2 id="date">2026-08-26</h2><h3 id="model">Model</h3>'
     '<h2 id="older">2025-01-01</h2><h3 id="old-model">Old</h3></article>'),
    ("anthropic", '<a href="/news/model">Aug 26, 2026 Announcements '
     '<h3>Model</h3></a>'),
]


def collector(source, html):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=html, request=request)
    ))
    return PageCollector(client)


@pytest.mark.parametrize("source_id,html", CASES)
def test_real_layouts_extract_and_filter_publisher_dates(source_id, html):
    source = SOURCES[source_id]
    c = collector(source, html)
    rows = c.collect(source, datetime(2026, 1, 1, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].title == "Model"
    assert rows[0].published_at == datetime(2026, 8, 26, tzinfo=UTC)
    assert c.collect(source, datetime(2026, 9, 1, tzinfo=UTC)) == []


@pytest.mark.parametrize("source_id,html", CASES)
def test_missing_dates_are_not_treated_as_current_news(source_id, html):
    broken = html.replace("2026-08-26", "unknown").replace("August 26, 2026", "unknown")
    broken = broken.replace("Aug 26, 2026", "unknown").replace("2025-01-01", "unknown")
    source = SOURCES[source_id]
    with pytest.raises(PageParseError):
        collector(source, broken).collect(source, datetime(2024, 1, 1, tzinfo=UTC))


def test_deepseek_does_not_borrow_title_from_next_section():
    source = SOURCES["deepseek"]
    html = '<article><h2 id="new">2026-08-26</h2><p>No title</p>'
    html += '<h2 id="old">2025-01-01</h2><h3 id="model">Old</h3></article>'
    rows = collector(source, html).collect(source, datetime(2024, 1, 1, tzinfo=UTC))
    assert len(rows) == 1 and rows[0].published_at.year == 2025


def test_duplicate_kimi_menu_and_list_cards_are_deduplicated():
    source_id, html = CASES[0]
    source = SOURCES[source_id]
    assert len(collector(source, html + html).collect(
        source, datetime(2024, 1, 1, tzinfo=UTC)
    )) == 1
