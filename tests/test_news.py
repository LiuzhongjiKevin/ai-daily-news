from datetime import UTC, datetime, timedelta
from typing import Literal

from ai_daily.models import RawItem
from ai_daily.news import canonicalize_url, fingerprint, prepare_news

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def item(
    source_id: str,
    source_type: Literal["official", "media", "release", "discovery"],
    title: str,
    url: str,
    *,
    published_at: datetime = NOW,
    excerpt: str | None = None,
    category: str = "model",
) -> RawItem:
    return RawItem(
        source_id=source_id,
        source_name=source_id,
        source_type=source_type,
        title=title,
        published_at=published_at,
        canonical_url=url,
        excerpt=title if excerpt is None else excerpt,
        language="zh",
        category=category,
    )


def test_canonicalize_url_removes_tracking_but_preserves_meaningful_query() -> None:
    """Would catch tracking identifiers surviving URL normalization or useful parameters being lost."""
    assert canonicalize_url(
        "HTTPS://Example.TEST//news//model/?utm_source=mail&ref=front&lang=zh#overview"
    ) == "https://example.test/news/model?lang=zh"
    assert canonicalize_url("https://EXAMPLE.test/") == "https://example.test/"


def test_fingerprint_normalizes_unicode_title_and_excerpt() -> None:
    """Would catch full-width Unicode variants creating duplicate event fingerprints."""
    standard = item(
        "one",
        "media",
        "Kimi 发布 K3 模型",
        "https://one.test/k3",
        excerpt="模型已发布",
    )
    full_width = item(
        "two",
        "media",
        "Ｋｉｍｉ 发布 K3 模型",
        "https://two.test/k3",
        excerpt="模型已发布",
    )

    assert fingerprint(standard) == fingerprint(full_width)
    assert len(fingerprint(standard)) == 64


def test_official_and_media_versions_form_one_a_grade_cluster() -> None:
    """Would catch verified releases being split from their matching media coverage."""
    clusters = prepare_news(
        [
            item("openai", "official", "OpenAI releases GPT-5", "https://openai.test/gpt-5"),
            item(
                "coverage",
                "media",
                "OpenAI officially releases GPT-5",
                "https://media.test/openai-gpt-5",
                excerpt="Independent coverage of the release.",
            ),
        ]
    )

    assert len(clusters) == 1
    assert clusters[0].trust_grade == "A"
    assert {row.source_id for row in clusters[0].items} == {"openai", "coverage"}


def test_bilingual_near_duplicate_titles_cluster_when_their_fingerprints_differ() -> None:
    """Would catch mixed Chinese-English headlines bypassing fuzzy event clustering."""
    clusters = prepare_news(
        [
            item(
                "first-media",
                "media",
                "Kimi 发布 K3 模型",
                "https://first.test/k3",
                excerpt="K3 模型今天亮相。",
            ),
            item(
                "second-media",
                "media",
                "Kimi 正式发布 K3 模型",
                "https://second.test/k3",
                excerpt="这项发布引发行业关注。",
            ),
        ]
    )

    assert len(clusters) == 1
    assert clusters[0].trust_grade == "B"


def test_similar_headlines_more_than_72_hours_apart_remain_separate() -> None:
    """Would catch an old report being merged into a new event outside the time window."""
    clusters = prepare_news(
        [
            item(
                "first",
                "media",
                "OpenAI releases GPT-5",
                "https://first.test/gpt-5",
                published_at=NOW,
            ),
            item(
                "second",
                "media",
                "OpenAI officially releases GPT-5",
                "https://second.test/gpt-5",
                published_at=NOW - timedelta(hours=72, seconds=1),
            ),
        ]
    )

    assert len(clusters) == 2


def test_similar_headlines_exactly_72_hours_apart_cluster() -> None:
    """Would catch the inclusive 72-hour clustering boundary being treated as exclusive."""
    clusters = prepare_news(
        [
            item("first", "media", "OpenAI releases GPT-5", "https://first.test/gpt-5"),
            item(
                "second",
                "media",
                "OpenAI officially releases GPT-5",
                "https://second.test/gpt-5",
                published_at=NOW - timedelta(hours=72),
            ),
        ]
    )

    assert len(clusters) == 1


def test_fuzzy_cluster_chain_never_spans_more_than_72_hours() -> None:
    """Would catch 0/71/142-hour fuzzy links being transitively merged into one event."""
    rows = [
        item(
            "recent",
            "media",
            "OpenAI releases GPT-5",
            "https://recent.test/gpt-5",
            excerpt="Recent report.",
        ),
        item(
            "middle",
            "media",
            "OpenAI releases GPT-5",
            "https://middle.test/gpt-5",
            published_at=NOW - timedelta(hours=71),
            excerpt="Middle report.",
        ),
        item(
            "old",
            "media",
            "OpenAI releases GPT-5",
            "https://old.test/gpt-5",
            published_at=NOW - timedelta(hours=142),
            excerpt="Old report.",
        ),
    ]

    clusters = prepare_news(rows)

    assert {frozenset(member.source_id for member in cluster.items) for cluster in clusters} == {
        frozenset({"recent"}),
        frozenset({"middle", "old"}),
    }
    assert all(
        max(member.published_at for member in cluster.items)
        - min(member.published_at for member in cluster.items)
        <= timedelta(hours=72)
        for cluster in clusters
    )


def test_exact_canonical_url_matches_even_when_their_dates_exceed_fuzzy_window() -> None:
    """Would catch exact source duplicates being weakened by the fuzzy-event time constraint."""
    clusters = prepare_news(
        [
            item(
                "recent",
                "media",
                "OpenAI release roundup",
                "https://example.test/gpt-5?utm_source=mail",
                excerpt="A recent source copy.",
            ),
            item(
                "old",
                "media",
                "Earlier reporting on the release",
                "https://EXAMPLE.test/gpt-5?ref=archive",
                published_at=NOW - timedelta(hours=142),
                excerpt="An older source copy.",
                category="research",
            ),
        ]
    )

    assert len(clusters) == 1
    assert {member.source_id for member in clusters[0].items} == {"recent", "old"}


def test_sub_threshold_titles_remain_separate() -> None:
    """Would catch unrelated same-category headlines being over-clustered."""
    clusters = prepare_news(
        [
            item("openai", "media", "OpenAI releases GPT-5", "https://news.test/gpt-5"),
            item("anthropic", "media", "Anthropic releases Claude", "https://news.test/claude"),
        ]
    )

    assert len(clusters) == 2


def test_two_distinct_media_domains_receive_b_grade() -> None:
    """Would catch independently corroborated media coverage being assigned the lowest trust grade."""
    clusters = prepare_news(
        [
            item(
                "first-media",
                "media",
                "AI startup raises funding",
                "https://first.test/funding",
                category="funding",
            ),
            item(
                "second-media",
                "media",
                "AI startup raises major funding",
                "https://second.test/funding",
                category="funding",
            ),
        ]
    )

    assert len(clusters) == 1
    assert clusters[0].trust_grade == "B"


def test_single_media_item_receives_c_grade() -> None:
    """Would catch uncorroborated coverage being promoted without another media domain."""
    clusters = prepare_news([item("media", "media", "AI startup raises funding", "https://media.test/funding")])

    assert clusters[0].trust_grade == "C"


def test_output_is_capped_at_requested_limit() -> None:
    """Would catch the candidate cap being ignored by downstream digest preparation."""
    rows = [
        item(
            str(number),
            "official",
            f"Model release {number}",
            f"https://example.test/{number}",
            category=f"candidate-{number}",
        )
        for number in range(20)
    ]

    assert len(prepare_news(rows, limit=12)) == 12


def test_equal_score_clusters_are_stably_sorted_by_cluster_id() -> None:
    """Would catch nondeterministic digest ordering when score and publication time tie."""
    clusters = prepare_news(
        [
            item("second", "official", "Model release B", "https://example.test/b"),
            item("first", "official", "Model release A", "https://example.test/a"),
        ]
    )

    assert [cluster.cluster_id for cluster in clusters] == sorted(cluster.cluster_id for cluster in clusters)


def test_empty_input_returns_no_clusters() -> None:
    """Would catch empty collection runs producing a fabricated news event."""
    assert prepare_news([]) == []
