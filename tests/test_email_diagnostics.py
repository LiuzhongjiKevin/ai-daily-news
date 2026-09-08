from pathlib import Path

from ai_daily.cost import CostReport
from ai_daily.models import Digest
from ai_daily.render import render_digest

COST = CostReport(currency="USD", total=0, thirty_day_projection=0, by_stage={})


def test_source_errors_hidden_in_both_mail_formats_but_kept_in_archive():
    warnings = [
        "glm-blog: HTTP 404 content-type=text/html url=https://z.ai/blog",
        "doubao: PageParseError: page selector mismatch",
        "36kr-discovery: RemoteProtocolError url=https://example.com/",
    ]
    digest = Digest(local_date="2026-09-07", news=[], repositories=[], warnings=warnings)
    rendered = render_digest(digest, COST, Path(__file__).parents[1] / "templates")
    for mail in (rendered.html, rendered.text):
        assert "数据提示" not in mail
        for warning in warnings:
            assert warning not in mail
    assert "glm-blog" in rendered.markdown
    assert "doubao" in rendered.markdown
    assert digest.warnings == warnings


def test_useful_cached_data_warning_still_visible():
    digest = Digest(local_date="2026-09-07", news=[], repositories=[], warnings=[
        "glm-blog: HTTP 404", "GitHub data unavailable; using cached snapshot from 2026-09-06"
    ])
    rendered = render_digest(digest, COST, Path(__file__).parents[1] / "templates")
    assert "using cached snapshot" in rendered.html
    assert "using cached snapshot" in rendered.text
    assert "glm-blog" not in rendered.html
