from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl


class AppSettings(BaseModel):
    timezone: str = "Asia/Shanghai"
    ai_mode: Literal["full", "economy", "off"] = "off"
    ai_base_url: HttpUrl = "https://api.deepseek.com"
    ai_model: str = "deepseek-v4-flash"
    ai_max_output_tokens: int = Field(default=2500, ge=256, le=8192)
    max_news_candidates: int = Field(default=12, ge=1, le=12)
    min_news_score: float = Field(default=40.0, ge=0, le=100)
    github_top_n: int = Field(default=10, ge=1, le=10)
    github_window_days: int = 7
    snapshot_retention_days: int = 35


class ModelPrice(BaseModel):
    input_cache_hit: float = Field(ge=0)
    input_cache_miss: float = Field(ge=0)
    output: float = Field(ge=0)


class PricingTable(BaseModel):
    effective_date: date
    currency: str = "USD"
    models: dict[str, ModelPrice]


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_settings(root: Path) -> AppSettings:
    return AppSettings.model_validate(_read_yaml(root / "config/settings.yaml"))


def load_prices(root: Path) -> PricingTable:
    return PricingTable.model_validate(_read_yaml(root / "config/pricing.yaml"))
