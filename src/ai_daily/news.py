"""Deterministic normalization, clustering, trust grading, and ranking for news items."""

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz.fuzz import token_set_ratio

from ai_daily.models import NewsCluster, RawItem

TRACKING_PARAMETERS = {"spm", "from", "ref"}
TRUST_POINTS = {"A": 40, "B": 25, "C": 10}
CATEGORY_POINTS = {
    "model": 25,
    "product": 25,
    "api": 25,
    "funding": 20,
    "ipo": 20,
    "acquisition": 20,
    "regulation": 20,
    "security": 20,
    "research": 15,
    "chips": 15,
    "open_source": 15,
}


def normalize_text(value: str) -> str:
    """Normalize Unicode and whitespace for deterministic textual comparisons."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def canonicalize_url(url: str) -> str:
    """Remove tracking noise while preserving the identity-bearing portions of a URL."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    retained = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not name.casefold().startswith("utm_") and name.casefold() not in TRACKING_PARAMETERS
    ]
    query = urlencode(sorted(retained))
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def fingerprint(item: RawItem) -> str:
    """Return a content fingerprint for exact duplicate detection."""
    material = f"{normalize_text(item.title)}\n{normalize_text(item.excerpt)[:500]}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _item_sort_key(item: RawItem) -> tuple[float, str, str, str]:
    return (-_utc(item.published_at).timestamp(), normalize_text(item.title), item.source_id, str(item.canonical_url))


def _within_cluster_window(first: RawItem, second: RawItem) -> bool:
    return abs((_utc(first.published_at) - _utc(second.published_at)).total_seconds()) <= 72 * 3600


def _cluster_id(items: list[RawItem]) -> str:
    identities = sorted(f"{canonicalize_url(str(item.canonical_url))}\n{fingerprint(item)}" for item in items)
    return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()


def cluster_items(items: list[RawItem]) -> list[list[RawItem]]:
    """Group exact duplicates first, then same-category, recent fuzzy title matches."""
    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    canonical_urls = [canonicalize_url(str(item.canonical_url)) for item in items]
    fingerprints = [fingerprint(item) for item in items]
    for first in range(len(items)):
        for second in range(first):
            if canonical_urls[first] == canonical_urls[second] or fingerprints[first] == fingerprints[second]:
                union(first, second)

    for first, item in enumerate(items):
        for second in range(first):
            fuzzy_match = (
                item.category == items[second].category
                and _within_cluster_window(item, items[second])
                and token_set_ratio(normalize_text(item.title), normalize_text(items[second].title)) >= 82
            )
            if fuzzy_match:
                union(first, second)

    grouped: dict[int, list[RawItem]] = {}
    for index, item in enumerate(items):
        grouped.setdefault(find(index), []).append(item)
    return [sorted(group, key=_item_sort_key) for group in grouped.values()]


def grade_cluster(items: list[RawItem]) -> Literal["A", "B", "C"]:
    """Assign source-verification trust grades to one cluster."""
    if any(item.source_type in {"official", "release"} for item in items):
        return "A"
    media_domains = {
        urlsplit(canonicalize_url(str(item.canonical_url))).hostname
        for item in items
        if item.source_type == "media"
    }
    return "B" if len(media_domains) >= 2 else "C"


def score_cluster(cluster: NewsCluster, now: datetime) -> float:
    """Score one cluster with source trust, category importance, and a 36-hour age decay."""
    newest = max(_utc(item.published_at) for item in cluster.items)
    age_hours = max(0.0, (_utc(now) - newest).total_seconds() / 3600)
    recency = max(0.0, 20.0 * (1.0 - age_hours / 36.0))
    category = max(CATEGORY_POINTS.get(item.category, 5) for item in cluster.items)
    return TRUST_POINTS[cluster.trust_grade] + category + recency


def prepare_news(items: list[RawItem], limit: int = 12) -> list[NewsCluster]:
    """Create a stably ordered, capped set of verified news clusters."""
    now = datetime.now(UTC)
    clusters: list[NewsCluster] = []
    for members in cluster_items(items):
        cluster = NewsCluster(
            cluster_id=_cluster_id(members),
            title=members[0].title,
            items=members,
            trust_grade=grade_cluster(members),
        )
        clusters.append(cluster.model_copy(update={"score": score_cluster(cluster, now)}))

    clusters.sort(
        key=lambda cluster: (
            -cluster.score,
            -max(_utc(item.published_at).timestamp() for item in cluster.items),
            cluster.cluster_id,
        )
    )
    return clusters[:max(0, limit)]
