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
CANONICAL_EVENT_CATEGORIES = frozenset({*CATEGORY_POINTS, "other"})
SOURCE_CATEGORY_ALIASES = {
    "hardware": "chips",
    "chip": "chips",
    "open-source": "open_source",
    "open source": "open_source",
}
MULTIPART_PUBLIC_SUFFIXES = frozenset({"co.jp", "co.uk", "com.au", "com.br", "com.cn", "com.sg"})

# Specific risk/transaction events win over broad technical nouns in the same headline.
# Within the technical group, evidence-bearing categories precede generic model/product terms.
CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ipo", ("ipo", "initial public offering", "上市", "招股")),
    (
        "acquisition",
        ("acquisition", "acquisitions", "acquire", "acquires", "acquired", "merger", "mergers", "收购", "并购", "合并"),
    ),
    ("funding", ("funding", "financing", "fundraise", "fundraising", "raises", "raised", "融资", "募资")),
    (
        "regulation",
        ("regulation", "regulations", "regulator", "regulatory", "policy", "law", "监管", "政策", "法案", "条例"),
    ),
    (
        "security",
        ("security", "vulnerability", "vulnerabilities", "breach", "attack", "安全", "漏洞", "攻击", "泄露"),
    ),
    ("chips", ("chip", "chips", "gpu", "gpus", "semiconductor", "semiconductors", "芯片", "算力", "半导体")),
    ("open_source", ("open source", "open-source", "open_source", "开源")),
    ("api", ("api", "apis", "sdk", "接口")),
    ("research", ("research", "paper", "papers", "benchmark", "benchmarks", "研究", "论文", "基准")),
    ("model", ("model", "models", "llm", "llms", "gpt", "claude", "gemini", "模型", "大模型")),
    (
        "product",
        ("product", "products", "feature", "features", "app", "apps", "assistant", "产品", "功能", "应用", "助手"),
    ),
)


def normalize_text(value: str) -> str:
    """Normalize Unicode and whitespace for deterministic textual comparisons."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_keyword(text: str, keyword: str) -> bool:
    if not keyword.isascii():
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def classify_event_category(item: RawItem) -> str:
    """Classify one event from bilingual evidence, using source taxonomy only as fallback."""
    evidence = normalize_text(f"{item.title}\n{item.excerpt}")
    for category, keywords in CATEGORY_KEYWORDS:
        if any(_contains_keyword(evidence, keyword) for keyword in keywords):
            return category
    source_category = normalize_text(item.category)
    alias = SOURCE_CATEGORY_ALIASES.get(source_category, source_category.replace("-", "_"))
    return alias if alias in CANONICAL_EVENT_CATEGORIES else "other"


def _publisher_domain(url: str) -> str:
    hostname = (urlsplit(canonicalize_url(url)).hostname or "").casefold().strip(".")
    labels = hostname.split(".")
    if len(labels) < 3:
        return hostname
    suffix_length = 2 if ".".join(labels[-2:]) in MULTIPART_PUBLIC_SUFFIXES else 1
    return ".".join(labels[-(suffix_length + 1) :])


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


def _items_fit_fuzzy_window(items: list[RawItem]) -> bool:
    published_at = [_utc(item.published_at) for item in items]
    return (max(published_at) - min(published_at)).total_seconds() <= 72 * 3600


def _fuzzy_match(first: RawItem, second: RawItem) -> bool:
    return (
        first.category == second.category
        and _within_cluster_window(first, second)
        and token_set_ratio(normalize_text(first.title), normalize_text(second.title)) >= 82
    )


def _groups_fuzzy_match(first: list[RawItem], second: list[RawItem]) -> bool:
    return any(_fuzzy_match(first_item, second_item) for first_item in first for second_item in second)


def _cluster_id(items: list[RawItem]) -> str:
    identities = sorted(f"{canonicalize_url(str(item.canonical_url))}\n{fingerprint(item)}" for item in items)
    return hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest()


def cluster_items(items: list[RawItem]) -> list[list[RawItem]]:
    """Group exact duplicates first, then form bounded fuzzy event clusters.

    Exact URL/fingerprint groups are retained regardless of their internal publication span.
    Fuzzy merging only joins those exact groups when the resulting component spans no more
    than 72 hours, so linked pairs cannot chain separate recurring events together.
    """
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

    grouped: dict[int, list[RawItem]] = {}
    for index, item in enumerate(items):
        grouped.setdefault(find(index), []).append(item)
    exact_groups = [sorted(group, key=_item_sort_key) for group in grouped.values()]
    exact_groups.sort(key=lambda group: (min(_utc(item.published_at) for item in group), _cluster_id(group)))

    fuzzy_parents = list(range(len(exact_groups)))
    fuzzy_members = [list(group) for group in exact_groups]

    def find_fuzzy(index: int) -> int:
        while fuzzy_parents[index] != index:
            fuzzy_parents[index] = fuzzy_parents[fuzzy_parents[index]]
            index = fuzzy_parents[index]
        return index

    for first in range(len(exact_groups)):
        for second in range(first + 1, len(exact_groups)):
            first_root, second_root = find_fuzzy(first), find_fuzzy(second)
            if first_root == second_root:
                continue
            combined = fuzzy_members[first_root] + fuzzy_members[second_root]
            if _groups_fuzzy_match(fuzzy_members[first_root], fuzzy_members[second_root]) and _items_fit_fuzzy_window(
                combined
            ):
                fuzzy_parents[second_root] = first_root
                fuzzy_members[first_root] = sorted(combined, key=_item_sort_key)

    return [
        fuzzy_members[index]
        for index in range(len(fuzzy_members))
        if find_fuzzy(index) == index
    ]


def grade_cluster(items: list[RawItem]) -> Literal["A", "B", "C"]:
    """Assign source-verification trust grades to one cluster."""
    if any(item.source_type in {"official", "release"} for item in items):
        return "A"
    publisher_domains = {
        _publisher_domain(str(item.canonical_url))
        for item in items
        if item.source_type in {"media", "discovery"}
    }
    publisher_domains.discard("")
    return "B" if len(publisher_domains) >= 2 else "C"


def score_cluster(cluster: NewsCluster, now: datetime) -> float:
    """Score one cluster with source trust, category importance, and a 36-hour age decay."""
    newest = max(_utc(item.published_at) for item in cluster.items)
    age_hours = max(0.0, (_utc(now) - newest).total_seconds() / 3600)
    recency = max(0.0, 20.0 * (1.0 - age_hours / 36.0))
    category = max(CATEGORY_POINTS.get(item.category, 5) for item in cluster.items)
    return TRUST_POINTS[cluster.trust_grade] + category + recency


def prepare_news(
    items: list[RawItem],
    limit: int = 12,
    *,
    min_score: float = 0.0,
    now: datetime | None = None,
) -> list[NewsCluster]:
    """Create a stably ordered, capped set of verified news clusters."""
    scoring_time = now or datetime.now(UTC)
    normalized_items = [
        item.model_copy(update={"category": classify_event_category(item)}) for item in items
    ]
    clusters: list[NewsCluster] = []
    for members in cluster_items(normalized_items):
        cluster = NewsCluster(
            cluster_id=_cluster_id(members),
            title=members[0].title,
            items=members,
            trust_grade=grade_cluster(members),
        )
        scored = cluster.model_copy(update={"score": score_cluster(cluster, scoring_time)})
        if scored.score >= min_score:
            clusters.append(scored)

    clusters.sort(
        key=lambda cluster: (
            -cluster.score,
            -max(_utc(item.published_at).timestamp() for item in cluster.items),
            cluster.cluster_id,
        )
    )
    return clusters[:max(0, limit)]
