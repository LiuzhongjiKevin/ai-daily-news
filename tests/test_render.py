from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from ai_daily.config import ModelPrice, PricingTable
from ai_daily.cost import calculate_cost
from ai_daily.models import Digest, NewsCluster, RankedRepo, RawItem, RepoSnapshot, UsageRecord
from ai_daily.render import render_digest


def _item(
    title: str,
    url: str,
    *,
    source_name: str = "OpenAI",
    published_at: datetime = datetime(2026, 8, 24, 6, 30, tzinfo=UTC),
) -> RawItem:
    return RawItem(
        source_id=source_name.lower().replace(" ", "-"),
        source_name=source_name,
        source_type="official",
        title=title,
        published_at=published_at,
        canonical_url=url,
        excerpt="Primary source excerpt.",
        language="en",
        category="model",
    )


def _digest(*, usage: list[UsageRecord] | None = None) -> Digest:
    collected_at = datetime(2026, 8, 24, 7, 0, tzinfo=UTC)
    snapshot = RepoSnapshot(
        repository="owner/repo",
        description="A <b>useful</b> repository",
        primary_language="Python",
        stars=12_345,
        forks=234,
        updated_at=collected_at,
        collected_at=collected_at,
    )
    return Digest(
        local_date="2026-08-24",
        news=[
            NewsCluster(
                cluster_id="a",
                title="可信要闻",
                items=[
                    _item("官方公告", "https://example.com/official"),
                    _item("Supporting report", "https://example.net/report", source_name="Research Lab"),
                ],
                trust_grade="A",
                summary="摘要内容",
                why_it_matters="重要性说明",
            ),
            NewsCluster(
                cluster_id="b",
                title="B 级要闻",
                items=[_item("B source", "https://example.org/b")],
                trust_grade="B",
            ),
            NewsCluster(
                cluster_id="c",
                title="C 级要闻",
                items=[_item("C source", "https://example.edu/c")],
                trust_grade="C",
            ),
        ],
        repositories=[RankedRepo(snapshot=snapshot, stars_gained=987, is_trial=True, explanation="值得关注")],
        warnings=["Source example.test is delayed"],
        usage=usage or [],
    )


def _cost(usage: list[UsageRecord]) -> object:
    prices = PricingTable(
        effective_date=date(2026, 8, 24),
        models={"deepseek-chat": ModelPrice(input_cache_hit=0, input_cache_miss=1, output=2)},
    )
    return calculate_cost(usage, prices)


@pytest.fixture
def templates_dir() -> Path:
    return Path(__file__).parents[1] / "templates"


def test_renders_subject_all_formats_and_digest_details(templates_dir: Path) -> None:
    """Would catch missing digest content across one of the three delivery formats."""
    usage = [
        UsageRecord(
            stage="news",
            model="deepseek-chat",
            input_cache_hit_tokens=10,
            input_cache_miss_tokens=20,
            output_tokens=30,
        )
    ]

    rendered = render_digest(_digest(usage=usage), _cost(usage), templates_dir)

    assert rendered.subject == "[AI Daily] 2026-08-24｜3 条 AI 要闻 + GitHub 周榜 Top 1"
    assert "可信要闻" in rendered.html
    assert "可信要闻" in rendered.text
    assert "可信要闻" in rendered.markdown
    assert "A" in rendered.html and "B" in rendered.html and "C" in rendered.html
    assert "https://example.com/official" in rendered.text
    assert "https://example.net/report" in rendered.text
    assert "试行排名" in rendered.html
    assert "Python" in rendered.markdown
    assert "12,345" in rendered.html
    assert "+987" in rendered.text
    assert "2026-08-24T07:00:00+00:00" in rendered.markdown
    assert "Source example.test is delayed" in rendered.html
    assert "Token" in rendered.text
    assert "30-day" in rendered.markdown
    assert "## GitHub" in rendered.markdown


def test_escapes_content_and_refuses_unsafe_links(templates_dir: Path) -> None:
    """Would catch fetched or model text becoming executable HTML or a clickable unsafe URL."""
    digest = _digest()
    digest.news[0].title = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    object.__setattr__(digest.news[0].items[0], "canonical_url", 'javascript:alert("x")')

    rendered = render_digest(digest, _cost([]), templates_dir)

    assert '<script>alert("x")</script>' not in rendered.html
    assert "&lt;script&gt;alert" in rendered.html
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered.html
    assert 'href="javascript:' not in rendered.html
    assert "javascript:alert" not in rendered.text
    assert "javascript:alert" not in rendered.markdown


def test_renders_empty_sections_and_omits_ai_cost_without_usage(templates_dir: Path) -> None:
    """Would catch an empty or off-mode digest emitting misleading section or cost content."""
    digest = Digest(local_date="2026-08-24", news=[], repositories=[], warnings=["No repository data"])

    rendered = render_digest(digest, _cost([]), templates_dir)

    assert rendered.subject.endswith("0 条 AI 要闻 + GitHub 周榜 Top 0")
    for output in (rendered.html, rendered.text, rendered.markdown):
        assert "今日无重大 AI 官方动态" in output
        assert "No repository data" in output
        assert "Token" not in output
        assert "30-day" not in output


def test_html_uses_outlook_safe_single_column_inline_layout(templates_dir: Path) -> None:
    """Would catch a template change that relies on unsafe email-client features."""
    rendered = render_digest(_digest(), _cost([]), templates_dir)
    html = rendered.html.lower()

    assert "max-width: 720px" in html
    assert "<table" in html
    assert "<script" not in html
    assert "<style" not in html
    assert "<link" not in html
    assert "@import" not in html
    assert "<form" not in html
    assert "font-family:" not in html


def test_strict_undefined_rejects_incomplete_templates(tmp_path: Path) -> None:
    """Would catch a missing template field being silently rendered as an empty value."""
    for name in ("daily.html.j2", "daily.txt.j2", "daily.md.j2"):
        (tmp_path / name).write_text("{{ missing_value }}", encoding="utf-8")

    with pytest.raises(UndefinedError):
        render_digest(_digest(), _cost([]), tmp_path)
