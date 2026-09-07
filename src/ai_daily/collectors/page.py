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
        if source.page_format != "standard":
            document = self._normalize_updates(document, source.page_format)
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
        rows = list({str(item.canonical_url): item for item in parsed_cards
                     if item.published_at >= cutoff}.values())
        return rows

    @staticmethod
    def _normalize_updates(document: BeautifulSoup, page_format: str) -> BeautifulSoup:
        """Adapt verified publisher layouts, retaining their explicit dates and permalinks."""
        output = BeautifulSoup("", "html.parser")

        def add(title: str, timestamp: str, href: str, excerpt: str = "") -> None:
            card = output.new_tag("article")
            for tag, value in (("h2", title), ("time", timestamp), ("p", excerpt)):
                node = output.new_tag(tag)
                node.string = value.replace("\u200b", "").strip()
                card.append(node)
            link = output.new_tag("a", href=href)
            card.append(link)
            output.append(card)

        if page_format in {"minimax_news", "sensetime_news", "mistral_news", "databricks_news"}:
            paths = {
                "minimax_news": "/blog/", "sensetime_news": "/cn/news/",
                "mistral_news": "/news/", "databricks_news": "/blog/",
            }
            for link in document.select("a[href]"):
                if not urlparse(link["href"]).path.startswith(paths[page_format]):
                    continue
                title_node = link.select_one("strong" if page_format == "sensetime_news"
                                             else "h2, h3, h4")
                date_node = link.select_one("time")
                stamp = (date_node.get("datetime") or date_node.get_text(" ", strip=True)
                         if date_node else "")
                if not stamp and page_format in {"minimax_news", "mistral_news"}:
                    pattern = (r"\b\d{4}-\d{2}-\d{2}\b" if page_format == "minimax_news"
                               else r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b")
                    match = re.search(pattern, link.get_text(" ", strip=True))
                    stamp = match[0] if match else ""
                if title_node and stamp:
                    excerpt = link.select_one("p")
                    add(title_node.get_text(" ", strip=True), stamp, link["href"],
                        excerpt.get_text(" ", strip=True) if excerpt else "")
        elif page_format == "stability_news":
            # Squarespace also wraps the entire list in an article: only use leaf cards.
            for card in document.select("article"):
                if card.select_one("article"):
                    continue
                title = card.select_one("h1 a[href], h2 a[href], h3 a[href]")
                date_node = card.select_one("time")
                if title and date_node:
                    stamp = date_node.get("datetime") or date_node.get_text(" ", strip=True)
                    add(title.get_text(" ", strip=True), stamp, title["href"])
        elif page_format == "glm_updates":
            for card in document.select(".update-container[id]"):
                stamp = card.get("id", "")
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
                    continue
                parts = [s.strip("\u200b ") for s in card.stripped_strings]
                parts = [s for s in parts if s and s != stamp]
                if parts:
                    add(parts[0], stamp, "#" + stamp, " ".join(parts[1:])[:2000])
        elif page_format == "deepseek_updates":
            for heading in document.select("article h2[id]"):
                stamp = re.search(r"\b\d{4}-\d{2}-\d{2}\b", heading.get_text())
                if not stamp:
                    continue
                # Bound each date section to avoid assigning the next release's title/date.
                for sibling in heading.next_siblings:
                    if not isinstance(sibling, Tag):
                        continue
                    if sibling.name == "h2":
                        break
                    if sibling.name == "h3" and sibling.get("id"):
                        add(sibling.get_text(" ", strip=True), stamp[0], "#" + sibling["id"])
        elif page_format == "anthropic_news":
            for link in document.select('a[href^="/news/"]'):
                text = link.get_text(" ", strip=True)
                stamp = re.search(r"\b[A-Z][a-z]{2} \d{1,2}, \d{4}\b", text)
                if not stamp:
                    continue
                title_node = link.select_one("h2, h3, h4")
                title = title_node.get_text(" ", strip=True) if title_node else text.replace(stamp[0], "")
                title = re.sub(r"^\s*(Announcements|Product|Policy|Research)\s+", "", title).strip()
                if title:
                    add(title, stamp[0], link["href"])
        return output

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
