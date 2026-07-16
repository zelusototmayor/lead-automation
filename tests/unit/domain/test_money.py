from __future__ import annotations

from decimal import Decimal

import pytest

from src.crm.domain.money import (
    MoneyDimension,
    MoneyStatus,
    aggregate_money,
    normalize_currency,
    normalize_money,
)


@pytest.mark.parametrize(
    "raw",
    (None, "", "  \t\n ", "unknown", "12,34", "1e2", object(), True, False, 1.5),
)
def test_unknown_blank_malformed_bool_and_float_values_are_missing(raw: object) -> None:
    result = normalize_money(raw, currency="eur", confirmed=True)

    assert result.amount is None
    assert result.currency == "EUR"
    assert result.status is MoneyStatus.MISSING


def test_missing_result_does_not_echo_the_raw_value() -> None:
    raw = "sensitive-$-malformed-value"

    result = normalize_money(raw, currency="EUR")

    assert raw not in repr(result)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        (Decimal("12.34"), Decimal("12.34")),
        (Decimal("12.3"), Decimal("12.30")),
        (12, Decimal("12.00")),
        ("12", Decimal("12.00")),
        ("+12.3", Decimal("12.30")),
        ("-0.01", Decimal("-0.01")),
        ("9999999999999999.99", Decimal("9999999999999999.99")),
        ("-9999999999999999.99", Decimal("-9999999999999999.99")),
    ),
)
def test_decimal_int_and_strict_decimal_strings_are_accepted(
    raw: Decimal | int | str, expected: Decimal
) -> None:
    result = normalize_money(raw, currency="EUR")

    assert result.amount == expected
    assert result.status is MoneyStatus.CANDIDATE


@pytest.mark.parametrize(
    "raw",
    (
        Decimal("1.230"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "1.230",
        ".50",
        "1.",
        " 1.00 ",
        "１.００",
        "10000000000000000.00",
        "-10000000000000000.00",
        10000000000000000,
    ),
)
def test_out_of_bounds_non_finite_and_inexact_scale_values_are_missing(
    raw: object,
) -> None:
    result = normalize_money(raw, currency="EUR", confirmed=True)

    assert result.amount is None
    assert result.status is MoneyStatus.MISSING


def test_confirmed_zero_remains_canonical_zero_and_confirmed() -> None:
    result = normalize_money(Decimal("-0.00"), currency="eur", confirmed=True)

    assert result.amount == Decimal("0.00")
    assert result.amount.as_tuple().sign == 0
    assert result.status is MoneyStatus.CONFIRMED


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("eur", "EUR"),
        (" USD ", "USD"),
        (None, None),
        ("", None),
        ("EU", None),
        ("EURO", None),
        ("€€€", None),
        ("ÉUR", None),
        (123, None),
    ),
)
def test_currency_is_three_ascii_letters_or_missing(
    raw: object, expected: str | None
) -> None:
    assert normalize_currency(raw) == expected


def test_aggregation_never_mixes_currencies_or_value_dimensions() -> None:
    values = (
        normalize_money(
            "10", currency="eur", dimension=MoneyDimension.ONE_OFF, confirmed=True
        ),
        normalize_money(
            "2", currency="EUR", dimension=MoneyDimension.ONE_OFF, confirmed=True
        ),
        normalize_money(
            "5", currency="usd", dimension=MoneyDimension.ONE_OFF, confirmed=True
        ),
        normalize_money(
            "3", currency="eur", dimension=MoneyDimension.MRR, confirmed=True
        ),
        normalize_money(
            "36", currency="eur", dimension=MoneyDimension.ARR, confirmed=True
        ),
        normalize_money("100", currency="eur", dimension=MoneyDimension.ONE_OFF),
        normalize_money("not-money", currency="eur", dimension=MoneyDimension.ONE_OFF),
        normalize_money("99", currency="invalid", dimension=MoneyDimension.ONE_OFF),
    )

    assert aggregate_money(values) == {
        (MoneyDimension.ONE_OFF, "EUR"): Decimal("12.00"),
        (MoneyDimension.ONE_OFF, "USD"): Decimal("5.00"),
        (MoneyDimension.MRR, "EUR"): Decimal("3.00"),
        (MoneyDimension.ARR, "EUR"): Decimal("36.00"),
    }
