from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import yaml
from dateutil import parser
from pydantic import BaseModel, Field, HttpUrl

from ai_daily.models import RawItem


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
    allowed_domains: list[str] = Field(default_factory=list)


class Collector(Protocol):
    def collect(self, source: SourceConfig, since: datetime) -> list[RawItem]: ...


def load_sources(path: Path) -> list[SourceConfig]:
    raw = yaml.safe_load(path.read_text("utf-8"))
    return [SourceConfig.model_validate(item) for item in raw["sources"] if item.get("enabled", True)]


def parse_datetime(value: datetime | str) -> datetime:
    """Return a UTC-aware timestamp, treating naïve publisher times as UTC."""
    parsed = parser.parse(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def require_since_aware(since: datetime) -> datetime:
    return parse_datetime(since)


class CollectorRegistry:
    def __init__(self, collectors: dict[str, Collector]) -> None:
        self.collectors = collectors

    def collect_all(
        self, sources: list[SourceConfig], since: datetime
    ) -> tuple[list[RawItem], list[str]]:
        items: list[RawItem] = []
        warnings: list[str] = []
        for source in sources:
            try:
                items.extend(self.collectors[source.kind].collect(source, since))
            except Exception as exc:  # noqa: BLE001 - isolate every source failure at this boundary
                warnings.append(f"{source.id}: {type(exc).__name__}")
        return items, warnings
