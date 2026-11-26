"""Unit tests for the Revolut account statement CSV parser (synthetic data only)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from opensteuerauszug.importers.revolut.csv_parser import (
    RevolutParseError,
    detect_decimal_comma,
    is_revolut_csv,
    load_revolut_csv,
    parse_amount,
    parse_timestamp,
    to_swiss_local,
)

HEADER = "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate"


def _write(tmp_path, text, name="revolut.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Time zone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utc, expected_local_date",
    [
        # Winter (CET, UTC+1): 23:30 UTC on New Year's Eve is already next year.
        (datetime(2024, 12, 31, 23, 30, tzinfo=UTC), date(2025, 1, 1)),
        (datetime(2024, 12, 31, 22, 59, tzinfo=UTC), date(2024, 12, 31)),
        # Summer (CEST, UTC+2): 22:30 UTC is after midnight in Zurich.
        (datetime(2024, 7, 1, 22, 30, tzinfo=UTC), date(2024, 7, 2)),
        (datetime(2024, 7, 1, 21, 59, tzinfo=UTC), date(2024, 7, 1)),
    ],
)
def test_utc_timestamps_are_dated_in_swiss_local_time(utc, expected_local_date):
    assert to_swiss_local(utc).date() == expected_local_date


def test_summer_time_switches_at_one_utc_on_last_sunday_of_march_and_october():
    # 2024: DST from 31 March 01:00 UTC to 27 October 01:00 UTC.
    assert to_swiss_local(datetime(2024, 3, 31, 0, 59, tzinfo=UTC)).hour == 1
    assert to_swiss_local(datetime(2024, 3, 31, 1, 0, tzinfo=UTC)).hour == 3
    assert to_swiss_local(datetime(2024, 10, 27, 0, 59, tzinfo=UTC)).hour == 2
    assert to_swiss_local(datetime(2024, 10, 27, 1, 0, tzinfo=UTC)).hour == 2
    assert to_swiss_local(datetime(2024, 10, 27, 1, 0, tzinfo=UTC)).utcoffset().seconds == 3600


@pytest.mark.parametrize(
    "value",
    [
        "2024-01-04T14:39:38.123456Z",
        "2024-01-04T14:39:38.123Z",
        "2024-01-04T14:39:38Z",
        "2024-01-04T14:39:38.123456789Z",
        "2024-01-04T15:39:38+01:00",
        "2024-01-04 14:39:38",
        "04.01.2024 14:39:38",
    ],
)
def test_supported_timestamp_formats_are_read_as_utc(value):
    parsed = parse_timestamp(value)
    assert parsed.replace(microsecond=0) == datetime(2024, 1, 4, 14, 39, 38, tzinfo=UTC)


def test_unrecognised_timestamp_is_rejected():
    with pytest.raises(RevolutParseError):
        parse_timestamp("01/04/2024")


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, currency, decimal_comma, expected",
    [
        ("USD 1,234.50", "USD", False, Decimal("1234.50")),
        ("USD -0.09", "USD", False, Decimal("-0.09")),
        ("$1,500", "USD", False, Decimal(1500)),
        ("-$30.93", "USD", False, Decimal("-30.93")),
        ("€50", "EUR", False, Decimal(50)),
        ("12,50 €", "EUR", True, Decimal("12.50")),
        ("1.234,50 EUR", "EUR", True, Decimal("1234.50")),
        ("1000", "USD", False, Decimal(1000)),
    ],
)
def test_amounts_with_currency_prefixes_and_symbols_are_parsed(
    value, currency, decimal_comma, expected
):
    assert parse_amount(value, currency, decimal_comma) == expected


def test_amount_in_other_currency_than_row_is_rejected():
    with pytest.raises(RevolutParseError, match="row currency"):
        parse_amount("EUR 10", "USD")


def test_decimal_comma_is_detected_from_unambiguous_values():
    assert detect_decimal_comma(["0,76672417", "1,500"]) is True
    assert detect_decimal_comma(["USD 222.54", "USD 1,500"]) is False
    assert detect_decimal_comma(["1,500"]) is False


def test_file_mixing_decimal_conventions_is_rejected():
    with pytest.raises(RevolutParseError):
        detect_decimal_comma(["0,5", "0.5"])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_standard_export_is_read_row_by_row(tmp_path):
    path = _write(
        tmp_path,
        HEADER
        + "\n"
        + "2024-03-01T14:00:00.000Z,AAPL,BUY - MARKET,2.5,USD 180.00,\"USD 450.00\",USD,1.10\n"
        + "2024-03-02T04:00:00.000Z,,CUSTODY FEE,,,USD -0.09,USD,1.10\n",
    )

    rows = load_revolut_csv(path)

    assert len(rows) == 2
    buy, fee = rows
    assert buy.ticker == "AAPL"
    assert buy.type == "BUY - MARKET"
    assert buy.quantity == Decimal("2.5")
    assert buy.price_per_share == Decimal("180.00")
    assert buy.total_amount == Decimal("450.00")
    assert buy.currency == "USD"
    assert buy.fx_rate == Decimal("1.10")
    assert buy.line == 2
    assert fee.ticker is None
    assert fee.quantity is None
    assert fee.total_amount == Decimal("-0.09")


def test_thousands_separators_in_standard_export_are_not_read_as_decimals(tmp_path):
    path = _write(
        tmp_path,
        HEADER + "\n2024-03-01T14:00:00Z,QCOM,BUY - LIMIT,50,USD 134.50,\"USD 6,725\",USD,1.36\n",
    )

    (row,) = load_revolut_csv(path)

    assert row.total_amount == Decimal(6725)


def test_semicolon_export_with_decimal_commas_is_read(tmp_path):
    path = _write(
        tmp_path,
        HEADER.replace(",", ";")
        + "\n2024-03-01T14:00:00Z;AAPL;BUY - MARKET;0,76672417;180,00 $;138,01 $;USD;1,10\n",
    )

    (row,) = load_revolut_csv(path)

    assert row.quantity == Decimal("0.76672417")
    assert row.total_amount == Decimal("138.01")
    assert row.fx_rate == Decimal("1.10")


def test_export_with_bom_and_reordered_columns_is_read_by_name(tmp_path):
    header = "﻿Ticker,Date,Type,Total Amount,Currency,Quantity,Price per share,FX Rate"
    path = _write(tmp_path, header + "\nAAPL,2024-03-01T14:00:00Z,DIVIDEND,USD 1.70,USD,,,1.1\n")

    (row,) = load_revolut_csv(path)

    assert row.type == "DIVIDEND"
    assert row.total_amount == Decimal("1.70")


def test_export_without_header_assumes_standard_column_order(tmp_path, caplog):
    path = _write(tmp_path, "2024-03-01T14:00:00Z,AAPL,DIVIDEND,,,USD 1.70,USD,1.1\n")

    (row,) = load_revolut_csv(path)

    assert row.ticker == "AAPL"
    assert row.line == 1
    assert "no header row" in caplog.text


def test_export_missing_a_required_column_is_rejected(tmp_path):
    path = _write(tmp_path, "Date,Ticker,Type\n2024-03-01T14:00:00Z,AAPL,DIVIDEND\n")

    with pytest.raises(RevolutParseError, match="missing required column"):
        load_revolut_csv(path)


@pytest.mark.parametrize("content", ["", HEADER + "\n"])
def test_empty_export_is_rejected(tmp_path, content):
    path = _write(tmp_path, content)

    with pytest.raises(RevolutParseError, match="empty"):
        load_revolut_csv(path)


def test_invalid_row_reports_file_and_line(tmp_path):
    path = _write(tmp_path, HEADER + "\n2024-03-01T14:00:00Z,AAPL,DIVIDEND,,,USD abc,USD,1\n")

    with pytest.raises(RevolutParseError, match="line 2"):
        load_revolut_csv(path)


def test_revolut_export_is_recognised_by_its_header(tmp_path):
    revolut = _write(tmp_path, HEADER + "\n", "a.csv")
    other = _write(tmp_path, "symbol,isin,valor\n", "b.csv")

    assert is_revolut_csv(revolut)
    assert not is_revolut_csv(other)
