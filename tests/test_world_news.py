from datetime import UTC, datetime, timedelta

from ai_daily.models import RawItem
from ai_daily.world_news import render_world, select_news

NOW = datetime(2026, 9, 9, 0, tzinfo=UTC)


def item(title, number=1, category="world", source="bbc", hours=1):
    return RawItem(source_id=source, source_name=source, source_type="media", title=title,
                   canonical_url=f"https://example.com/{number}", category=category,
                   published_at=NOW - timedelta(hours=hours), excerpt="Original source excerpt")


def test_cutoff_future_duplicates_and_off_topic():
    rows = [item("Diplomatic peace agreement"), item("Diplomatic peace agreement", 2),
            item("War ceasefire", 3, hours=25), item("Trade sanctions", 4, hours=-1),
            item("Local cooking recipes", 5)]
    assert [str(x.canonical_url) for x in select_news(rows, NOW)] == ["https://example.com/1"]


def test_section_caps_and_source_diversity():
    rows = [item(f"Trade sanctions imposed on country {n}", n) for n in range(10)]
    rows += [item(f"Central bank inflation decision {n}", n+20, "finance") for n in range(10)]
    rows += [item("Diplomatic summit ends war", 50, source="un")]
    selected = select_news(rows, NOW)
    assert len(selected) <= 12
    assert sum(x.category == "world" for x in selected) <= 6
    assert sum(x.category == "finance" for x in selected) <= 6
    assert "un" in {x.source_id for x in selected}


def test_render_escapes_external_text_and_discloses_gaps():
    rendered = render_world([item('<script>alert(1)</script> War agreement')], NOW,
                            [{"source": "fed", "status": "failed", "count": 0}])
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html
    assert "2026-09-09" in rendered.subject
    assert "https://example.com/1" in rendered.text
    assert "原文" in rendered.text and "失败" in rendered.text
    assert "GitHub" not in rendered.text


def test_twelve_distinct_stories_are_capped_at_six_per_section():
    world = ["War ends after talks", "President visits China", "Election results in Europe",
             "Military withdraws from islands", "Nuclear treaty approved", "Refugee aid arrives",
             "Diplomatic summit announced"]
    finance = ["Inflation slows in Japan", "Oil production cut", "Central bank cuts rates",
               "Employment rises in Canada", "Gold price record", "Currency crisis continues",
               "Government debt downgraded"]
    rows = [item(title, n, "world") for n, title in enumerate(world)]
    rows += [item(title, n+20, "finance") for n, title in enumerate(finance)]
    selected = select_news(rows, NOW)
    assert len(selected) == 12
    assert sum(row.category == "world" for row in selected) == 6
    assert sum(row.category == "finance" for row in selected) == 6


def test_opinion_is_not_labelled_as_straight_reporting():
    rendered = render_world([item("Central bank outlook | Letter", category="finance")], NOW, [])
    assert "评论/分析" in rendered.text


def test_each_story_is_a_separate_email_card_with_original_below_title():
    from bs4 import BeautifulSoup

    class Translator:
        def translate(self, text):
            return '中文 <script>不可信</script>'

    rendered = render_world([item('Trade agreement', 1), item('Oil market', 2, 'finance')],
                            NOW, [], translator=Translator())
    document = BeautifulSoup(rendered.html, 'html.parser')
    cards = document.select('table.news-card')
    assert len(cards) == 2
    for card, original in zip(cards, ('Trade agreement', 'Oil market'), strict=True):
        assert original not in card.h3.get_text()
        assert original in card.select_one('.original-title').get_text()
        assert card.select_one('.news-source') is not None
        assert card.select_one('.news-time') is not None
        assert 'border:' in card['style']
        assert card.find('script') is None
    assert rendered.markdown.count('\n---\n') == 2
    assert rendered.text.count('─' * 40) == 2
