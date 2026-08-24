import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup

from ai_daily.collectors.base import SourceConfig, parse_datetime, require_since_aware
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem


def parse_entry_datetime(entry: Any) -> datetime:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            return parse_datetime(value)
    raise ValueError("feed entry has no published timestamp")


class FeedCollector:
    def __init__(self, client: RetryingClient) -> None:
        self.client = client

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        response = self.client.get(str(source.url))
        parsed = feedparser.parse(response.content)
        cutoff = require_since_aware(since)
        rows: list[RawItem] = []
        for entry in parsed.entries:
            try:
                published = parse_entry_datetime(entry)
                title = entry.title.strip()
                link = entry.link
            except (AttributeError, KeyError, ValueError):
                continue
            if published < cutoff or not title or not link:
                continue
            rows.append(
                RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=title,
                    published_at=published,
                    canonical_url=urljoin(str(source.url), link),
                    excerpt=re.sub(
                        r"\s+([.,;:!?])",
                        r"\1",
                        BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(
                            " ", strip=True
                        ),
                    ),
                    language=source.language,
                    category=source.category,
                )
            )
        return rows
