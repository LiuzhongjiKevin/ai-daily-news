from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from ai_daily.config import PricingTable
from ai_daily.models import UsageRecord

_MILLION = Decimal(1_000_000)
_SIX_DECIMALS = Decimal("0.000001")


class UnknownModelPriceError(ValueError):
    """Raised when usage names a model that is missing from the pricing table."""


class CostReport(BaseModel):
    currency: str
    total: Decimal
    thirty_day_projection: Decimal
    by_stage: dict[str, Decimal]
    is_complete: bool = True


def _quantize(amount: Decimal) -> Decimal:
    return amount.quantize(_SIX_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_cost(usage: list[UsageRecord], prices: PricingTable) -> CostReport:
    """Price recorded model usage using configured per-million-token rates."""
    by_stage: dict[str, Decimal] = {}
    for record in usage:
        try:
            price = prices.models[record.model]
        except KeyError as error:
            raise UnknownModelPriceError(f"No configured price for model {record.model!r}") from error

        amount = (
            Decimal(record.input_cache_hit_tokens) * Decimal(str(price.input_cache_hit))
            + Decimal(record.input_cache_miss_tokens) * Decimal(str(price.input_cache_miss))
            + Decimal(record.output_tokens) * Decimal(str(price.output))
        ) / _MILLION
        by_stage[record.stage] = by_stage.get(record.stage, Decimal()) + amount

    total = _quantize(sum(by_stage.values(), Decimal()))
    quantized_by_stage = {stage: _quantize(amount) for stage, amount in by_stage.items()}
    return CostReport(
        currency=prices.currency,
        total=total,
        thirty_day_projection=_quantize(total * Decimal(30)),
        by_stage=quantized_by_stage,
        is_complete=all(record.is_complete for record in usage),
    )
