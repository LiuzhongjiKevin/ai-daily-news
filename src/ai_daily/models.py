from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator

SafeAttemptId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class RawItem(BaseModel):
    source_id: str
    source_name: str
    source_type: Literal["official", "media", "release", "discovery"]
    title: str
    published_at: datetime
    canonical_url: HttpUrl
    excerpt: str = ""
    language: Literal["zh", "en", "other"] = "other"
    category: str = "other"
    content_fingerprint: str = ""


class NewsCluster(BaseModel):
    cluster_id: str
    title: str
    items: list[RawItem]
    trust_grade: Literal["A", "B", "C"]
    score: float = 0.0
    summary: str = ""
    why_it_matters: str = ""


class RepoSnapshot(BaseModel):
    repository: str
    description: str = ""
    primary_language: str | None = None
    stars: int = Field(ge=0)
    forks: int = Field(ge=0)
    updated_at: datetime
    archived: bool = False
    is_fork: bool = False
    collected_at: datetime


class RankedRepo(BaseModel):
    snapshot: RepoSnapshot
    stars_gained: int
    is_trial: bool
    explanation: str = ""


class UsageRecord(BaseModel):
    stage: str
    model: str
    call_count: int = Field(default=1, ge=1)
    input_cache_hit_tokens: int = Field(default=0, ge=0)
    input_cache_miss_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    is_complete: bool = True


class Digest(BaseModel):
    local_date: str
    news: list[NewsCluster]
    repositories: list[RankedRepo]
    warnings: list[str] = Field(default_factory=list)
    usage: list[UsageRecord] = Field(default_factory=list)
    estimated_cost: float = 0.0


class DeliveryIntent(BaseModel):
    attempt_id: SafeAttemptId
    created_at: datetime
    status: Literal["reserved", "ambiguous"] = "reserved"

    @field_validator("created_at")
    @classmethod
    def require_aware_creation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delivery intent creation time must be timezone-aware")
        return value


class RunState(BaseModel):
    last_success_at: datetime | None = None
    sent_dates: dict[str, str] = Field(default_factory=dict)
    delivery_intents: dict[str, DeliveryIntent] = Field(default_factory=dict)

    @field_validator("delivery_intents")
    @classmethod
    def require_iso_delivery_dates(
        cls, value: dict[str, DeliveryIntent]
    ) -> dict[str, DeliveryIntent]:
        for local_date in value:
            try:
                parsed = date.fromisoformat(local_date)
            except ValueError as error:
                raise ValueError("delivery intent date must use ISO format") from error
            if parsed.isoformat() != local_date:
                raise ValueError("delivery intent date must use ISO format")
        return value
