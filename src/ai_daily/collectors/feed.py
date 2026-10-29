import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup

from ai_daily.collectors.base import (
    FeedParseError,
    SourceConfig,
    parse_datetime,
    require_since_aware,
)
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem
from ai_daily.news import contains_any_keyword

# Feed furniture that carries no story content: hnrss link listings and WordPress footers.
_EXCERPT_BOILERPLATE = (
    re.compile(r"^Article URL:.*$", re.DOTALL),
    re.compile(r"\s*The post .+? appeared first on .+$", re.DOTALL),
)


def clean_excerpt(text: str) -> str:
    for pattern in _EXCERPT_BOILERPLATE:
        text = pattern.sub("", text)
    return text.strip()


def parse_entry_datetime(entry: Any, *, require_published: bool = False) -> datetime:
    keys = ("published",) if require_published else ("published", "updated", "created")
    for key in keys:
        value = entry.get(key)
        if value:
            return parse_datetime(value)
    raise ValueError("feed entry has no published timestamp")


class FeedCollector:
    def __init__(
        self, client: RetryingClient, now: Callable[[], datetime] | None = None
    ) -> None:
        self.client = client
        self.now = now or (lambda: datetime.now(UTC))

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        response = self.client.get(str(source.url))
        parsed = feedparser.parse(response.content)
        if not parsed.version:
            raise FeedParseError("invalid feed: unrecognised document")
        if parsed.bozo and not parsed.entries:
            error = parsed.get("bozo_exception")
            detail = type(error).__name__ if error is not None else "parser error"
            raise FeedParseError(f"invalid feed: {detail}")
        cutoff = require_since_aware(since)
        latest = parse_datetime(self.now()) + timedelta(minutes=5)
        rows: list[RawItem] = []
        for entry in parsed.entries:
            try:
                published = parse_entry_datetime(
                    entry, require_published=source.require_published_date
                )
                title = entry.title.strip()
                link = entry.link
            except (AttributeError, KeyError, ValueError):
                continue
            if published < cutoff or published > latest or not title or not link:
                continue
            if source.include_keywords and not contains_any_keyword(
                title, source.include_keywords
            ):
                continue
            canonical_url = urljoin(str(source.url), link)
            if source.allowed_link_hosts:
                parsed_link = urlsplit(canonical_url)
                if (
                    parsed_link.scheme != 'https'
                    or parsed_link.hostname not in source.allowed_link_hosts
                    or parsed_link.username or parsed_link.password
                    or parsed_link.port not in {None, 443}
                ):
                    continue
            rows.append(
                RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=title,
                    published_at=published,
                    canonical_url=canonical_url,
                    excerpt=clean_excerpt(re.sub(
                        r"\s+([.,;:!?])",
                        r"\1",
                        BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(
                            " ", strip=True
                        ),
                    )),
                    language=source.language,
                    category=source.category,
                )
            )
        return rows
