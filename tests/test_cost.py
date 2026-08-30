from datetime import date
from decimal import Decimal

import pytest

from ai_daily.config import ModelPrice, PricingTable
from ai_daily.cost import UnknownModelPriceError, calculate_cost
from ai_daily.models import UsageRecord


def prices() -> PricingTable:
    return PricingTable(
        effective_date=date(2026, 8, 24),
        models={
            "deepseek-v4-flash": ModelPrice(
                input_cache_hit=0.014,
                input_cache_miss=0.44,
                output=1.32,
            )
        },
    )


def test_calculates_current_model_cost_per_million_tokens_and_stage_totals() -> None:
    """Would catch cache-tier token usage being priced incorrectly or merged across stages."""
    usage = [
        UsageRecord(
            stage="news",
            model="deepseek-v4-flash",
            input_cache_hit_tokens=1_000_000,
            input_cache_miss_tokens=500_000,
            output_tokens=250_000,
        ),
        UsageRecord(
            stage="github",
            model="deepseek-v4-flash",
            input_cache_miss_tokens=1_000_000,
        ),
    ]

    report = calculate_cost(usage, prices())

    assert report.currency == "USD"
    assert report.by_stage == {
        "news": Decimal("0.564000"),
        "github": Decimal("0.440000"),
    }
    assert report.total == Decimal("1.004000")
    assert report.thirty_day_projection == Decimal("30.120000")


def test_usage_record_defaults_to_one_call_for_backward_compatibility() -> None:
    """Would catch legacy usage records losing their single-call meaning after schema extension."""
    record = UsageRecord(stage="news", model="deepseek-v4-flash")

    assert record.call_count == 1


def test_quantizes_totals_to_six_decimal_places() -> None:
    """Would catch sub-microdollar token prices being rounded inconsistently."""
    report = calculate_cost(
        [
            UsageRecord(
                stage="news",
                model="deepseek-v4-flash",
                input_cache_hit_tokens=1,
                input_cache_miss_tokens=1,
                output_tokens=1,
            )
        ],
        prices(),
    )

    assert report.by_stage == {"news": Decimal("0.000002")}
    assert report.total == Decimal("0.000002")
    assert report.thirty_day_projection == Decimal("0.000060")


def test_totals_are_rounded_after_aggregating_exact_stage_amounts() -> None:
    """Would catch total cost losing fractions by summing already-rounded stage subtotals."""
    report = calculate_cost(
        [
            UsageRecord(stage="news", model="deepseek-v4-flash", output_tokens=1),
            UsageRecord(stage="github", model="deepseek-v4-flash", output_tokens=1),
        ],
        prices(),
    )

    assert report.by_stage == {
        "news": Decimal("0.000001"),
        "github": Decimal("0.000001"),
    }
    assert report.total == Decimal("0.000003")


def test_rejects_usage_for_models_without_configured_prices() -> None:
    """Would catch unknown models being silently reported as free."""
    usage = [UsageRecord(stage="news", model="unknown-model", output_tokens=1)]

    with pytest.raises(UnknownModelPriceError, match="unknown-model"):
        calculate_cost(usage, prices())
