import re
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import httpx
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


class GitHubResponseError(RuntimeError):
    """A GitHub endpoint returned content that could not be parsed without exposing its body."""


class GitHubDataError(RuntimeError):
    """Parsed GitHub data violates the required ranking schema."""


class GitHubSnapshotBatch(list[RepoSnapshot]):
    """List-compatible snapshot result carrying redacted partial-degradation warnings."""

    def __init__(
        self,
        snapshots: Iterable[RepoSnapshot] = (),
        *,
        warnings: Iterable[str] = (),
    ) -> None:
        super().__init__(snapshots)
        self.warnings = tuple(warnings)


def _response_json(response: httpx.Response, message: str) -> object:
    invalid_response = False
    try:
        payload = response.json()
    except ValueError:
        invalid_response = True
        payload = None
    if invalid_response:
        raise GitHubResponseError(message)
    return payload


def github_headers(token: str) -> dict[str, str]:
    """Return the required headers for a GitHub API request."""
    headers = {
        "Accept": GITHUB_ACCEPT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
        payload = _response_json(response, "GitHub search response is invalid")
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise GitHubDataError("GitHub search data is invalid")
        for item in payload["items"]:
            full_name = item.get("full_name") if isinstance(item, dict) else None
            if not isinstance(full_name, str) or not full_name.strip():
                raise GitHubDataError("GitHub search data is invalid")
            add([full_name])
    return list(names.values())


def fetch_repo_snapshots(
    client: RetryingClient,
    token: str,
    names: Iterable[str],
    collected_at: datetime,
    *,
    max_repositories: int = MAX_CANDIDATE_REPOSITORIES,
) -> GitHubSnapshotBatch:
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
    failure_sources: Counter[str] = Counter()
    for start in range(0, len(selected), METADATA_BATCH_SIZE):
        for name in selected[start : start + METADATA_BATCH_SIZE]:
            try:
                response = client.get(
                    f"https://api.github.com/repos/{name}", headers=github_headers(token)
                )
            except httpx.HTTPStatusError as error:
                source = "http_404" if error.response.status_code == 404 else "http_status"
                failure_sources[source] += 1
                continue
            except httpx.RequestError:
                failure_sources["transport"] += 1
                continue

            try:
                payload = _response_json(response, "GitHub repository response is invalid")
            except GitHubResponseError:
                failure_sources["response"] += 1
                continue
            try:
                snapshot = _snapshot_from_metadata(payload, collected_at)
            except GitHubDataError:
                failure_sources["data"] += 1
                continue
            if not snapshot.archived and not snapshot.is_fork:
                snapshots.append(snapshot)
    warnings: list[str] = []
    if failure_sources:
        details = ", ".join(
            f"{source}={failure_sources[source]}" for source in sorted(failure_sources)
        )
        warnings.append(
            f"GitHub metadata partial: {sum(failure_sources.values())}/{len(selected)} "
            f"repositories unavailable ({details})"
        )
    return GitHubSnapshotBatch(snapshots, warnings=warnings)


def _snapshot_from_metadata(payload: object, collected_at: datetime) -> RepoSnapshot:
    if not isinstance(payload, dict):
        raise GitHubDataError("GitHub repository data is invalid")
    required_text = ("full_name", "updated_at")
    for key in required_text:
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise GitHubDataError("GitHub repository data is invalid")
    for key in ("stargazers_count", "forks_count"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GitHubDataError("GitHub repository data is invalid")
    for key in ("archived", "fork"):
        if not isinstance(payload.get(key), bool):
            raise GitHubDataError("GitHub repository data is invalid")
    description = payload.get("description")
    language = payload.get("language")
    if description is not None and not isinstance(description, str):
        raise GitHubDataError("GitHub repository data is invalid")
    if language is not None and not isinstance(language, str):
        raise GitHubDataError("GitHub repository data is invalid")
    mirror_url = payload.get("mirror_url")
    if mirror_url is not None and (
        not isinstance(mirror_url, str) or not mirror_url.strip()
    ):
        raise GitHubDataError("GitHub repository data is invalid")
    try:
        updated_at = datetime.fromisoformat(payload["updated_at"])
    except ValueError:
        raise GitHubDataError("GitHub repository data is invalid") from None
    if updated_at.tzinfo is None:
        raise GitHubDataError("GitHub repository data is invalid")
    return RepoSnapshot(
        repository=payload["full_name"],
        description=description or "",
        primary_language=language,
        stars=payload["stargazers_count"],
        forks=payload["forks_count"],
        updated_at=updated_at,
        archived=payload["archived"],
        is_fork=payload["fork"],
        is_mirror=mirror_url is not None,
        collected_at=collected_at,
    )


class GitHubReleaseCollector:
    def __init__(self, client: RetryingClient) -> None:
        self.client = client

    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]:
        response = self.client.get(
            self._releases_url(source),
            headers={"Accept": "application/vnd.github+json"},
        )
        try:
            releases = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown").split(";", 1)[0]
            raise SourceParseError(f"invalid GitHub releases JSON content-type={content_type}") from exc
        if not isinstance(releases, list):
            raise SourceParseError("GitHub releases response schema is invalid")
        cutoff = require_since_aware(since)
        rows: list[RawItem] = []
        valid_rows = 0
        for release in releases:
            if not isinstance(release, dict) or not isinstance(release.get("draft"), bool):
                continue
            published_value = release.get("published_at") or release.get("created_at")
            url = release.get("html_url")
            name = release.get("name")
            tag_name = release.get("tag_name")
            title = (
                name.strip()
                if isinstance(name, str) and name.strip()
                else tag_name.strip()
                if isinstance(tag_name, str)
                else ""
            )
            if (
                not isinstance(published_value, (str, datetime))
                or not isinstance(url, str)
                or not title
            ):
                continue
            try:
                published = parse_datetime(published_value)
                body = release.get("body")
                row = RawItem(
                    source_id=source.id,
                    source_name=source.name,
                    source_type=source.source_type,
                    title=title,
                    published_at=published,
                    canonical_url=url,
                    excerpt=re.sub(
                        r"\s+([.,;:!?])",
                        r"\1",
                        BeautifulSoup(body if isinstance(body, str) else "", "html.parser").get_text(
                            " ", strip=True
                        ),
                    ),
                    language=source.language,
                    category=source.category,
                )
            except (TypeError, ValueError):
                continue
            valid_rows += 1
            if release["draft"]:
                continue
            if published < cutoff:
                continue
            rows.append(row)
        if releases and valid_rows == 0:
            raise SourceParseError("GitHub releases response schema is invalid")
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
