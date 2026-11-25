"""Shared intermediate dataclasses and utilities for Trading212 data."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Optional, Tuple


def parse_t212_date(value: str) -> date:
    """Parse a T212 datetime string to a date.

    Handles ISO 8601 (``2024-01-15T10:30:00+00:00``) and the CSV
    format (``2024-01-15 10:30:00``).  Only the first 10 characters
    (the date portion) are used.
    """
    if not value or not value.strip():
        raise ValueError("Empty date string")
    return date.fromisoformat(value.strip()[:10])


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
    the instrument currency is ``quantity * gross_per_share`` when both are
    known.  ``withholding_tax`` is a positive amount in
    ``withholding_currency``.
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
