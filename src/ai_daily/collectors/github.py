import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ai_daily.collectors.base import (
    SourceConfig,
    SourceParseError,
    parse_datetime,
    require_since_aware,
)
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem


class GitHubReleaseCollector:
    def __init__(self, client: RetryingClient) -> None:
        self.client = client

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        response = self.client.get(self._releases_url(source), headers={"Accept": "application/vnd.github+json"})
        try:
            releases = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown").split(";", 1)[0]
            raise SourceParseError(f"invalid GitHub releases JSON content-type={content_type}") from exc
        if not isinstance(releases, list):
            raise TypeError("GitHub releases response is not a list")
        cutoff = require_since_aware(since)
        rows: list[RawItem] = []
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            published_value = release.get("published_at") or release.get("created_at")
            url = release.get("html_url")
            title = (release.get("name") or release.get("tag_name") or "").strip()
            if not published_value or not url or not title:
                continue
            try:
                published = parse_datetime(published_value)
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
                    excerpt=re.sub(
                        r"\s+([.,;:!?])",
                        r"\1",
                        BeautifulSoup(release.get("body") or "", "html.parser").get_text(
                            " ", strip=True
                        ),
                    ),
                    language=source.language,
                    category=source.category,
                )
            )
        return rows

    @staticmethod
    def _releases_url(source: SourceConfig) -> str:
        parsed = urlparse(str(source.url))
        if parsed.netloc == "api.github.com" and parsed.path.endswith("/releases"):
            return str(source.url)
        if parsed.netloc in {"github.com", "www.github.com"}:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                return f"https://api.github.com/repos/{parts[0]}/{parts[1]}/releases"
        raise ValueError("github_releases source URL must identify a GitHub repository")
