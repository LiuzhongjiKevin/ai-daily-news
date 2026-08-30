from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml
from dateutil import parser
from pydantic import BaseModel, Field, HttpUrl, ValidationError, model_validator

from ai_daily.models import RawItem


class SourceParseError(ValueError):
    """A source responded successfully but did not match its configured transport/shape."""


class SourceConfigurationError(ValueError):
    """The source configuration file is malformed or violates the source contract."""


class FeedParseError(SourceParseError):
    pass


class PageParseError(SourceParseError):
    pass


class SourceConfig(BaseModel):
    id: str
    name: str
    kind: Literal["feed", "page", "github_releases", "discovery"]
    source_type: Literal["official", "media", "release", "discovery"]
    url: HttpUrl
    language: Literal["zh", "en", "other"]
    category: str
    enabled: bool = True
    item_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    date_selector: str | None = None
    excerpt_selector: str | None = None
    link_path_pattern: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_page_contract(self) -> "SourceConfig":
        if self.kind == "page" and not self.link_path_pattern:
            raise ValueError("page sources require a link_path_pattern")
        return self


class Collector(Protocol):
    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]: ...


@dataclass(frozen=True, slots=True)
class SourceCollectionBatch:
    """Collected content plus source-boundary outcomes, without parsing warning text."""

    items: list[RawItem]
    warnings: list[str]
    source_successes: int
    source_total: int

    def __iter__(self) -> Iterator[list[RawItem] | list[str]]:
        # Preserve the existing two-value unpacking contract for internal callers.
        yield self.items
        yield self.warnings


def load_sources(path: Path) -> list[SourceConfig]:
    try:
        raw = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("top-level mapping required")
        entries = raw.get("sources")
        if not isinstance(entries, list):
            raise TypeError("sources list required")
        sources: list[SourceConfig] = []
        for item in entries:
            if not isinstance(item, dict):
                raise TypeError("source entry mapping required")
            if item.get("enabled", True):
                sources.append(SourceConfig.model_validate(item))
        return sources
    except (OSError, TypeError, ValidationError, ValueError, yaml.YAMLError) as error:
        raise SourceConfigurationError("Source configuration is invalid") from error


def parse_datetime(value: datetime | str) -> datetime:
    """Return a UTC-aware timestamp, treating naïve publisher times as UTC."""
    parsed = parser.parse(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def require_since_aware(since: datetime) -> datetime:
    return parse_datetime(since)


def format_collection_error(exc: Exception) -> str:
    """Return a concise, secret-safe source failure diagnostic without response bodies."""
    if isinstance(exc, httpx.HTTPStatusError):
        content_type = exc.response.headers.get("content-type", "unknown").split(";", 1)[0]
        return (
            f"HTTP {exc.response.status_code} content-type={content_type} "
            f"url={redact_url(str(exc.request.url))}"
        )
    if isinstance(exc, httpx.RequestError):
        return f"{type(exc).__name__} url={redact_url(str(exc.request.url))}"
    if isinstance(exc, SourceParseError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def format_source_failure(source: SourceConfig, exc: Exception) -> str:
    """Attach the configured endpoint to diagnostics that do not carry a request."""
    diagnostic = format_collection_error(exc)
    if " url=" not in diagnostic:
        return f"{diagnostic} url={redact_url(str(source.url))}"
    return diagnostic


def redact_url(url: str) -> str:
    """Keep the endpoint useful while ensuring query credentials never enter a warning."""
    parsed = urlsplit(url)
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return f"{base}?<redacted>" if parsed.query else base


class CollectorRegistry:
    def __init__(self, collectors: dict[str, Collector]) -> None:
        self.collectors = collectors

    def collect_all(
        self, sources: list[SourceConfig], since: datetime
    ) -> SourceCollectionBatch:
        items: list[RawItem] = []
        warnings: list[str] = []
        source_successes = 0
        for source in sources:
            try:
                items.extend(self.collectors[source.kind].collect(source, since))
                source_successes += 1
            except Exception as exc:  # noqa: BLE001 - isolate every source failure at this boundary
                warnings.append(f"{source.id}: {format_source_failure(source, exc)}")
        return SourceCollectionBatch(
            items=items,
            warnings=warnings,
            source_successes=source_successes,
            source_total=len(sources),
        )
