import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ai_daily.collectors.base import (
    SourceConfig,
    SourceParseError,
    parse_datetime,
    require_since_aware,
)
from ai_daily.http import RetryingClient
from ai_daily.models import RawItem, RepoSnapshot

GITHUB_API_VERSION = "2022-11-28"
GITHUB_ACCEPT = "application/vnd.github+json"
MAX_CANDIDATE_REPOSITORIES = 100
METADATA_BATCH_SIZE = 50


def github_headers(token: str) -> dict[str, str]:
    """Return the required headers for a GitHub API request."""
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def parse_trending(html: str) -> list[str]:
    """Extract owner/repository names from the repository links on a Trending page."""
    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    for link in soup.select("article h2 a[href], .Box-row h2 a[href]"):
        match = re.fullmatch(r"/([^/]+)/([^/]+)/?", link.get("href", "").strip())
        if match:
            names.append(f"{match.group(1)}/{match.group(2)}")
    return names


def discover_candidates(
    client: RetryingClient,
    token: str,
    today: date,
    historical_names: Iterable[str] = (),
) -> list[str]:
    """Collect candidates in priority order: Trending, retained history, then Search star order.

    The returned order is used by the 100-request metadata cap: daily Trending leads weekly
    Trending, then names retained from snapshots, then the created and pushed Search results in
    their API star order. Case-insensitive duplicates retain their earliest, highest-priority form.
    """
    names: dict[str, str] = {}

    def add(values: Iterable[str]) -> None:
        for value in values:
            name = value.strip()
            if name and "/" in name:
                names.setdefault(name.casefold(), name)

    add(parse_trending(client.get("https://github.com/trending?since=daily").text))
    add(parse_trending(client.get("https://github.com/trending?since=weekly").text))
    add(historical_names)
    queries = (
        f"created:>={today - timedelta(days=14)}",
        f"pushed:>={today - timedelta(days=7)} stars:>=100",
    )
    for query in queries:
        response = client.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": 100},
            headers=github_headers(token),
        )
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise TypeError("GitHub repository search response has no items list")
        for item in payload["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
                raise TypeError("GitHub repository search item has no full_name")
            add([item["full_name"]])
    return list(names.values())


def fetch_repo_snapshots(
    client: RetryingClient,
    token: str,
    names: Iterable[str],
    collected_at: datetime,
    *,
    max_repositories: int = MAX_CANDIDATE_REPOSITORIES,
) -> list[RepoSnapshot]:
    """Fetch metadata in candidate priority order through at most 100 GitHub API requests."""
    if max_repositories < 1:
        raise ValueError("max_repositories must be positive")
    unique: dict[str, str] = {}
    for name in names:
        normalized = name.strip()
        if normalized and "/" in normalized:
            unique.setdefault(normalized.casefold(), normalized)
    selected = list(unique.values())[: min(max_repositories, MAX_CANDIDATE_REPOSITORIES)]
    snapshots: list[RepoSnapshot] = []
    for start in range(0, len(selected), METADATA_BATCH_SIZE):
        for name in selected[start : start + METADATA_BATCH_SIZE]:
            response = client.get(
                f"https://api.github.com/repos/{name}", headers=github_headers(token)
            )
            snapshot = _snapshot_from_metadata(response.json(), collected_at)
            if not snapshot.archived and not snapshot.is_fork:
                snapshots.append(snapshot)
    return snapshots


def _snapshot_from_metadata(payload: object, collected_at: datetime) -> RepoSnapshot:
    if not isinstance(payload, dict):
        raise TypeError("GitHub repository response is not an object")
    required_text = ("full_name", "updated_at")
    for key in required_text:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise TypeError(f"GitHub repository response has invalid {key}")
    for key in ("stargazers_count", "forks_count"):
        if isinstance(payload.get(key), bool) or not isinstance(payload.get(key), int):
            raise TypeError(f"GitHub repository response has invalid {key}")
    for key in ("archived", "fork"):
        if not isinstance(payload.get(key), bool):
            raise TypeError(f"GitHub repository response has invalid {key}")
    description = payload.get("description")
    language = payload.get("language")
    if description is not None and not isinstance(description, str):
        raise ValueError("GitHub repository response has invalid description")
    if language is not None and not isinstance(language, str):
        raise ValueError("GitHub repository response has invalid language")
    try:
        updated_at = datetime.fromisoformat(payload["updated_at"])
    except ValueError as exc:
        raise ValueError("GitHub repository response has invalid updated_at") from exc
    if updated_at.tzinfo is None:
        raise ValueError("GitHub repository response has timezone-naive updated_at")
    return RepoSnapshot(
        repository=payload["full_name"],
        description=description or "",
        primary_language=language,
        stars=payload["stargazers_count"],
        forks=payload["forks_count"],
        updated_at=updated_at,
        archived=payload["archived"],
        is_fork=payload["fork"],
        collected_at=collected_at,
    )


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
