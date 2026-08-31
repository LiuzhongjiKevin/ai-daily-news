from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from ai_daily.collectors.base import (
    SourceConfig,
    SourceParseError,
    parse_datetime,
    require_since_aware,
)
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem

AI_KEYWORDS = '(AI OR "artificial intelligence" OR "人工智能" OR "大模型" OR LLM OR "生成式AI")'


class DiscoveryCollector:
    def __init__(self, client: RetryingClient, now: Callable[[], datetime] | None = None) -> None:
        self.client = client
        self.now = now or (lambda: datetime.now(UTC))

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        if not source.allowed_domains:
            raise ValueError("discovery sources require an explicit domain allowlist")
        end = self.now().astimezone(UTC)
        start = end - timedelta(hours=36)
        domain_query = " OR ".join(f"domain:{domain}" for domain in source.allowed_domains)
        response = self.client.get(
            str(source.url),
            params={
                "query": f"{AI_KEYWORDS} AND ({domain_query})",
                "mode": "ArtList",
                "format": "json",
                "maxrecords": 250,
                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown").split(";", 1)[0]
            raise SourceParseError(f"invalid discovery JSON content-type={content_type}") from exc
        if not isinstance(payload, dict) or "articles" not in payload:
            raise SourceParseError("discovery response schema is invalid")
        articles = payload["articles"]
        if not isinstance(articles, list):
            raise SourceParseError("discovery response schema is invalid")
        cutoff = require_since_aware(since)
        rows: list[RawItem] = []
        valid_rows = 0
        for article in articles:
            if not isinstance(article, dict):
                continue
            url = article.get("url")
            title_value = article.get("title")
            timestamp = article.get("seendate") or article.get("published_at")
            if (
                not isinstance(url, str)
                or not isinstance(title_value, str)
                or not title_value.strip()
                or not isinstance(timestamp, (str, datetime))
                or not self._is_allowed(url, source.allowed_domains)
            ):
                continue
            try:
                published = parse_datetime(timestamp)
                description = article.get("description") or article.get("snippet") or ""
                if not isinstance(description, str):
                    description = ""
                row = RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=title_value.strip(),
                    published_at=published,
                    canonical_url=url,
                    excerpt=description.strip(),
                    language=source.language,
                    category=source.category,
                )
            except (TypeError, ValueError):
                continue
            valid_rows += 1
            if published < cutoff:
                continue
            rows.append(row)
        if articles and valid_rows == 0:
            raise SourceParseError("discovery response schema is invalid")
        return rows

    @staticmethod
    def _is_allowed(url: str, allowed_domains: list[str]) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return any(
            hostname == domain.lower() or hostname.endswith(f".{domain.lower()}")
            for domain in allowed_domains
        )
