import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from ai_daily.collectors.base import (
    PageParseError,
    SourceConfig,
    parse_datetime,
    require_since_aware,
)
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem


def _origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _allowed_destination(source: SourceConfig, url: str) -> bool:
    source_origin = _origin(str(source.url))
    destination_origin = _origin(url)
    if source_origin is None or destination_origin is None:
        return False
    if destination_origin == source_origin:
        return True
    scheme, host, port = destination_origin
    return scheme == "https" and port == 443 and host in source.allowed_link_hosts


class PageCollector:
    def __init__(self, client: RetryingClient) -> None:
        self.client = client

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        selectors = (
            source.item_selector,
            source.title_selector,
            source.link_selector,
            source.date_selector,
        )
        if any(selector is None for selector in selectors):
            raise ValueError("page sources require item, title, link, and date selectors")
        if not source.link_path_pattern:
            raise ValueError("page sources require a link_path_pattern")
        try:
            link_path_pattern = re.compile(source.link_path_pattern)
        except re.error as exc:
            raise ValueError("page source link_path_pattern is invalid") from exc
        response = self.client.get(str(source.url))
        if _origin(str(response.url)) != _origin(str(source.url)):
            raise PageParseError("page final response origin is not allowed")
        document = BeautifulSoup(response.content, "html.parser")
        cutoff = require_since_aware(since)
        cards = document.select(source.item_selector)
        if not cards:
            raise PageParseError(f"page selector mismatch: {source.item_selector!r} matched 0 cards")
        parsed_cards: list[RawItem] = []
        for item in cards:
            parsed = self._parse_item(item, source, link_path_pattern)
            if parsed is not None:
                parsed_cards.append(parsed)
        if not parsed_cards:
            raise PageParseError(f"page selector mismatch: matched {len(cards)} cards but parsed 0")
        rows = [item for item in parsed_cards if item.published_at >= cutoff]
        return rows

    @staticmethod
    def _parse_item(item: Tag, source: SourceConfig, link_path_pattern: re.Pattern[str]) -> RawItem | None:
        assert source.title_selector and source.link_selector and source.date_selector
        title_node = item.select_one(source.title_selector)
        link_node = item.select_one(source.link_selector)
        date_node = item.select_one(source.date_selector)
        if title_node is None or link_node is None or date_node is None:
            return None
        href = link_node.get("href")
        timestamp = date_node.get("datetime") or date_node.get_text(" ", strip=True)
        if not href or not timestamp:
            return None
        canonical_url = urljoin(str(source.url), href)
        if not _allowed_destination(source, canonical_url):
            return None
        if not link_path_pattern.search(urlparse(canonical_url).path):
            return None
        try:
            published = parse_datetime(timestamp)
        except (TypeError, ValueError):
            return None
        title = title_node.get_text(" ", strip=True)
        if not title:
            return None
        excerpt = ""
        if source.excerpt_selector:
            excerpt_node = item.select_one(source.excerpt_selector)
            if excerpt_node is not None:
                excerpt = excerpt_node.get_text(" ", strip=True)
        try:
            return RawItem(
                source_id=source.id,
                source_name=source.name,
                source_type=source.source_type,
                title=title,
                published_at=published,
                canonical_url=canonical_url,
                excerpt=excerpt,
                language=source.language,
                category=source.category,
            )
        except ValidationError:
            return None
