"""Shared intermediate dataclasses and utilities for Trading212 data."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal, Optional, Tuple
from zoneinfo import ZoneInfo

# Swiss tax periods follow local (Swiss) time; Trading212 states times in UTC.
SWISS_TZ = ZoneInfo("Europe/Zurich")


def parse_t212_date(value: str) -> date:
    """Parse a T212 timestamp to the Swiss calendar date.

    Handles the CSV formats (``2024-01-15 10:30:00``, with optional
    milliseconds, from the ``Time`` or ``Time (UTC)`` column) and ISO 8601
    from the API (``2024-01-15T10:30:00.123+00:00`` / ``...Z``).  Trading212
    states every time in UTC, so a value without a zone is read as UTC.  The
    result is the date in Europe/Zurich, so a trade at 23:30 UTC on
    31 December falls into the next Swiss tax year.  A bare date is returned
    as is.
    """
    if not value or not value.strip():
        raise ValueError("Empty date string")
    text = value.strip()
    if len(text) == 10:
        return date.fromisoformat(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Unknown time suffix: fall back to the date part.
        return date.fromisoformat(text[:10])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SWISS_TZ).date()


@dataclass
class T212Order:
    """Represents a single filled buy or sell order from Trading212."""

    filled_at: date
    side: Literal["BUY", "SELL"]
    ticker: str
    name: str
    isin: Optional[str]
    quantity: Decimal
    price: Decimal
    currency: str  # instrument/price currency
    fx_rate: Optional[Decimal]  # instrument currency → account currency
    total_account_currency: Decimal  # net value in account currency
    is_split: bool = False  # quantity-only mutation from a stock split row
    # quantity-only transfer / stock distribution (not in the API order history)
    is_share_movement: bool = False


@dataclass
class T212CashTransaction:
    """Represents a cash-level transaction (interest, lending income)."""

    transaction_date: date
    action: str  # e.g. "Interest on cash", "Lending interest"
    amount: Decimal
    currency: str


@dataclass
class T212Dividend:
    """Represents a dividend payment from Trading212.

    ``amount_account_currency`` is the *net* amount credited to the account
    (T212 "Total"), denominated in ``account_currency``.  The gross amount in
    the instrument currency is ``quantity * gross_per_share`` (API) or
    ``quantity * net_per_share + withholding_tax`` (CSV).
    ``withholding_tax`` is a positive amount in ``withholding_currency``.
    """

    paid_on: date
    ticker: str
    name: str
    isin: Optional[str]
    amount_account_currency: Decimal
    gross_per_share: Optional[Decimal]
    withholding_tax: Optional[Decimal]
    instrument_currency: str
    fx_rate: Optional[Decimal]
    quantity: Optional[Decimal] = None
    account_currency: str = ""
    # CSV "Price / share" on a dividend row: per share *after* withholding.
    net_per_share: Optional[Decimal] = None
    withholding_currency: str = ""
    action: str = "Dividend"


def normalize_currency(amount: Optional[Decimal], currency: str) -> Tuple[Optional[Decimal], str]:
    """Convert minor-unit quotes to their ISO major currency.

    Trading212 quotes LSE instruments in pence (``GBX``/``GBp``) and some
    South African ones in cents (``ZAc``); eCH-0196 and the Kursliste only know
    ISO 4217 codes.
    """
    minor_units = {"GBX": "GBP", "GBP_PENCE": "GBP", "ZAC": "ZAR", "ILA": "ILS"}
    code = (currency or "").strip()
    if code == "GBp" or code.upper() in minor_units:
        major = "GBP" if code == "GBp" else minor_units[code.upper()]
        return (amount / Decimal(100) if amount is not None else None), major
    return amount, code.upper()
