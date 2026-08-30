from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


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


class RunState(BaseModel):
    last_success_at: datetime | None = None
    sent_dates: dict[str, str] = Field(default_factory=dict)
