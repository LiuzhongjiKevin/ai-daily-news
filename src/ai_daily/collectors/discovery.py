from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from ai_daily.collectors.base import SourceConfig, parse_datetime, require_since_aware
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
        payload = response.json()
        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        cutoff = require_since_aware(since)
        rows: list[RawItem] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            url = article.get("url")
            title = (article.get("title") or "").strip()
            timestamp = article.get("seendate") or article.get("published_at")
            if not url or not title or not timestamp or not self._is_allowed(url, source.allowed_domains):
                continue
            try:
                published = parse_datetime(timestamp)
            except (TypeError, ValueError):
                continue
            if published < cutoff:
                continue
            rows.append(
                RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=title,
                    published_at=published,
                    canonical_url=url,
                    excerpt=(article.get("description") or article.get("snippet") or "").strip(),
                    language=source.language,
                    category=source.category,
                )
            )
        return rows

    @staticmethod
    def _is_allowed(url: str, allowed_domains: list[str]) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return any(
            hostname == domain.lower() or hostname.endswith(f".{domain.lower()}")
            for domain in allowed_domains
        )
