from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final, Iterable


class MoneyStatus(str, Enum):
    MISSING = "missing"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class MoneyDimension(str, Enum):
    ONE_OFF = "one_off"
    MRR = "mrr"
    ARR = "arr"


@dataclass(frozen=True, slots=True)
class MoneyValue:
    amount: Decimal | None
    currency: str | None
    dimension: MoneyDimension
    status: MoneyStatus


_CENTS: Final[Decimal] = Decimal("0.01")
_MAX_AMOUNT: Final[Decimal] = Decimal("9999999999999999.99")
_STRICT_DECIMAL: Final[re.Pattern[str]] = re.compile(
    r"[+-]?[0-9]+(?:\.[0-9]{1,2})?", re.ASCII
)
_CURRENCY: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]{3}", re.ASCII)


def normalize_currency(value: object) -> str | None:
    """Return an uppercase three-letter ASCII currency code, or missing."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if _CURRENCY.fullmatch(normalized) is None:
        return None
    return normalized.upper()


def _decimal_amount(value: object) -> Decimal | None:
    if type(value) is int:
        amount = Decimal(value)
    elif isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str) and _STRICT_DECIMAL.fullmatch(value) is not None:
        amount = Decimal(value)
    else:
        return None

    if not amount.is_finite():
        return None
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        return None
    if not -_MAX_AMOUNT <= amount <= _MAX_AMOUNT:
        return None

    normalized = amount.quantize(_CENTS)
    return Decimal("0.00") if normalized.is_zero() else normalized


def normalize_money(
    value: object,
    *,
    currency: object = None,
    dimension: MoneyDimension = MoneyDimension.ONE_OFF,
    confirmed: bool = False,
) -> MoneyValue:
    """Normalize one monetary dimension without retaining malformed source data."""
    amount = _decimal_amount(value)
    if amount is None:
        status = MoneyStatus.MISSING
    elif confirmed is True:
        status = MoneyStatus.CONFIRMED
    else:
        status = MoneyStatus.CANDIDATE
    return MoneyValue(
        amount=amount,
        currency=normalize_currency(currency),
        dimension=dimension,
        status=status,
    )


def aggregate_money(
    values: Iterable[MoneyValue],
) -> dict[tuple[MoneyDimension, str], Decimal]:
    """Total confirmed values only within the same dimension and known currency."""
    totals: dict[tuple[MoneyDimension, str], Decimal] = {}
    for value in values:
        if (
            value.status is not MoneyStatus.CONFIRMED
            or value.amount is None
            or value.currency is None
        ):
            continue
        key = (value.dimension, value.currency)
        totals[key] = totals.get(key, Decimal("0.00")) + value.amount
    return totals
