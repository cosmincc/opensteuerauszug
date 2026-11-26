"""Parser for the Revolut Invest account statement CSV.

Revolut exports one CSV per investment account, covering whatever period the
user selected (ideally *All time*). The standard layout is::

    Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate
    2024-01-04T14:39:38.123456Z,MSFT,BUY - MARKET,4.49,USD 222.54,USD 1000,USD,1.36

This module only turns the file into typed :class:`RevolutRow` records. The
tax interpretation lives in :mod:`revolut_importer`.

Variants handled (seen in the wild or reported by other tools):

* Comma, semicolon or tab delimiters and an optional UTF-8 BOM.
* A missing header row (the standard column order is then assumed).
* Amounts with an ISO code prefix (``USD 1,234.50``), a currency symbol
  (``$1,234.50``, ``-$30.93``, ``12,50 €``) or no currency at all.
* Decimal commas in quantities and amounts (``0,76672417``).
* ISO 8601 timestamps with or without fractional seconds / ``Z`` suffix,
  ``YYYY-MM-DD HH:MM:SS`` and ``DD.MM.YYYY HH:MM:SS``.

Timestamps are in UTC. They are converted to Swiss local time (CET/CEST)
before taking the calendar date, so a trade at 23:30 UTC on 31 December
belongs to the next tax year.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

logger = logging.getLogger(__name__)

COL_DATE = "Date"
COL_TICKER = "Ticker"
COL_TYPE = "Type"
COL_QUANTITY = "Quantity"
COL_PRICE = "Price per share"
COL_TOTAL = "Total Amount"
COL_CURRENCY = "Currency"
COL_FX_RATE = "FX Rate"

STANDARD_COLUMNS: list[str] = [
    COL_DATE,
    COL_TICKER,
    COL_TYPE,
    COL_QUANTITY,
    COL_PRICE,
    COL_TOTAL,
    COL_CURRENCY,
    COL_FX_RATE,
]
REQUIRED_COLUMNS = {COL_DATE, COL_TICKER, COL_TYPE, COL_QUANTITY, COL_TOTAL, COL_CURRENCY}

# Header aliases seen in other Revolut export variants, normalised to the
# standard names. Keys are compared case-insensitively with whitespace removed.
_HEADER_ALIASES: dict[str, str] = {
    "date": COL_DATE,
    "ticker": COL_TICKER,
    "symbol": COL_TICKER,
    "type": COL_TYPE,
    "quantity": COL_QUANTITY,
    "pricepershare": COL_PRICE,
    "totalamount": COL_TOTAL,
    "currency": COL_CURRENCY,
    "fxrate": COL_FX_RATE,
}

_CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "CHF": "CHF",
    "Fr.": "CHF",
}


class RevolutParseError(ValueError):
    """Raised when the Revolut CSV cannot be read safely."""


@dataclass(frozen=True)
class RevolutRow:
    """One row of the Revolut account statement."""

    source: str
    line: int
    timestamp: datetime  # timezone-aware, UTC
    ticker: str | None
    type: str
    quantity: Decimal | None
    price_per_share: Decimal | None
    total_amount: Decimal
    currency: str
    fx_rate: Decimal | None

    @property
    def local_date(self) -> date:
        """Calendar date of the row in Swiss local time."""
        return to_swiss_local(self.timestamp).date()

    @property
    def dedup_key(self) -> tuple:
        """Identity of the row independent of the file it came from."""
        return (
            self.timestamp,
            self.ticker,
            self.type,
            self.quantity,
            self.total_amount,
            self.currency,
        )


# ---------------------------------------------------------------------------
# Time zone handling
# ---------------------------------------------------------------------------


def _last_sunday(year: int, month: int) -> date:
    # Day 31 exists in both March and October.
    d = date(year, month, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def to_swiss_local(ts: datetime) -> datetime:
    """Convert an aware UTC datetime to Swiss local time (CET/CEST).

    Switzerland follows the EU summer-time rule (since 1981): CEST (UTC+2)
    from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last
    Sunday of October, CET (UTC+1) otherwise. Implemented directly so the
    importer does not depend on the platform's tz database (absent on
    Windows without the ``tzdata`` package).
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    utc = ts.astimezone(UTC)
    year = utc.year
    dst_start = datetime.combine(_last_sunday(year, 3), datetime.min.time(), UTC).replace(hour=1)
    dst_end = datetime.combine(_last_sunday(year, 10), datetime.min.time(), UTC).replace(hour=1)
    offset = timedelta(hours=2) if dst_start <= utc < dst_end else timedelta(hours=1)
    return utc.astimezone(timezone(offset))


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
)


def parse_timestamp(value: str) -> datetime:
    """Parse a Revolut timestamp into an aware UTC datetime.

    Values without a zone are taken as UTC (the export states UTC times).
    """
    raw = value.strip()
    if not raw:
        raise RevolutParseError("Empty date")
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    # Python < 3.12 strptime %f accepts at most 6 digits; trim nanoseconds.
    candidate = re.sub(r"(\.\d{6})\d+", r"\1", candidate)
    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)  # noqa: DTZ007 - zone set below
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    raise RevolutParseError(f"Unrecognised date format: {value!r}")


def _normalise_number(text: str, decimal_comma: bool = False) -> str:
    """Turn a localised number string into a plain ``Decimal`` literal.

    ``decimal_comma`` selects the file's convention (see
    :func:`detect_decimal_comma`); the other separator is a thousands mark.
    """
    s = text.replace("\u00a0", "").replace(" ", "").replace("'", "").replace("’", "")
    if decimal_comma:
        return s.replace(".", "").replace(",", ".")
    return s.replace(",", "")


_COMMA_DECIMAL_RE = re.compile(r"\d\.\d{3},\d|^[^.]*\d,(\d{1,2}|\d{4,})(\D*)$")
_DOT_DECIMAL_RE = re.compile(r"\d,\d{3}\.\d|^[^,]*\d\.(\d{1,2}|\d{4,})(\D*)$")


def detect_decimal_comma(values: list[str]) -> bool:
    """Decide whether a file writes numbers with a decimal comma.

    A single number such as ``1,500`` is ambiguous, so the decision is made
    once per file from the values that are not: ``0,76672417`` or
    ``1.234,50`` vote for a decimal comma, ``222.54`` or ``1,234.50`` for a
    decimal point. Without evidence either way the decimal point (the
    standard Revolut export) is assumed.
    """
    comma = dot = 0
    for value in values:
        v = value.strip()
        if not v:
            continue
        if _COMMA_DECIMAL_RE.search(v):
            comma += 1
        elif _DOT_DECIMAL_RE.search(v):
            dot += 1
    if comma and dot:
        raise RevolutParseError(
            "The file mixes decimal commas and decimal points; cannot read numbers safely."
        )
    return comma > 0


def parse_decimal(value: str, decimal_comma: bool = False) -> Decimal | None:
    """Parse a plain (currency-free) number; empty means ``None``."""
    raw = value.strip()
    if not raw:
        return None
    try:
        return Decimal(_normalise_number(raw, decimal_comma))
    except InvalidOperation as exc:
        raise RevolutParseError(f"Invalid number: {value!r}") from exc


def parse_amount(value: str, row_currency: str, decimal_comma: bool = False) -> Decimal | None:
    """Parse an amount that may carry a currency code or symbol.

    The currency found in the value must agree with ``row_currency``; a
    mismatch means the row cannot be interpreted safely.
    """
    raw = value.strip()
    if not raw:
        return None
    negative = False
    s = raw
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-"):
        negative, s = not negative, s[1:].strip()

    found_currency: str | None = None
    m = re.match(r"^([A-Z]{3})\s*(.*)$", s)
    if m:
        found_currency, s = m.group(1), m.group(2)
    else:
        m = re.match(r"^(.*?)\s*([A-Z]{3})$", s)
        if m and m.group(1):
            found_currency, s = m.group(2), m.group(1)
    if found_currency is None:
        for symbol in sorted(_CURRENCY_SYMBOLS, key=len, reverse=True):
            if s.startswith(symbol):
                found_currency, s = _CURRENCY_SYMBOLS[symbol], s[len(symbol) :].strip()
                break
            if s.endswith(symbol):
                found_currency, s = _CURRENCY_SYMBOLS[symbol], s[: -len(symbol)].strip()
                break
    if s.startswith("-"):
        negative, s = not negative, s[1:].strip()

    if found_currency is not None and row_currency and found_currency != row_currency:
        raise RevolutParseError(
            f"Amount {value!r} is in {found_currency} but the row currency is {row_currency}"
        )
    number = parse_decimal(s, decimal_comma)
    if number is None:
        raise RevolutParseError(f"Invalid amount: {value!r}")
    return -number if negative else number


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def _normalise_header(cell: str) -> str:
    key = re.sub(r"\s+", "", cell.lstrip("﻿")).lower()
    return _HEADER_ALIASES.get(key, cell.strip())


def _looks_like_header(cells: list[str]) -> bool:
    return any(_normalise_header(c) in REQUIRED_COLUMNS for c in cells) and not any(
        re.match(r"^\d{4}-\d{2}-\d{2}", c.strip()) for c in cells
    )


def _sniff_delimiter(sample: str) -> str:
    first_line = sample.splitlines()[0] if sample else ""
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def is_revolut_csv(path: Path) -> bool:
    """Return True if *path* has a Revolut account statement header."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            first = fh.readline()
    except (OSError, UnicodeDecodeError):
        return False
    cells = next(csv.reader([first], delimiter=_sniff_delimiter(first)), [])
    names = {_normalise_header(c) for c in cells}
    return REQUIRED_COLUMNS.issubset(names)


def load_revolut_csv(path: str | Path) -> list[RevolutRow]:
    """Read a Revolut account statement CSV into rows, in file order."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise RevolutParseError(f"Revolut CSV file {path} is empty")
    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        first = next(reader)
    except StopIteration as exc:  # pragma: no cover - guarded above
        raise RevolutParseError(f"Revolut CSV file {path} is empty") from exc

    first_line_no = 1
    if _looks_like_header(first):
        header = [_normalise_header(c) for c in first]
        pending: list[list[str]] = []
        first_line_no = 2
    else:
        if len(first) != len(STANDARD_COLUMNS):
            raise RevolutParseError(
                f"{path}: no header row and {len(first)} columns instead of "
                f"{len(STANDARD_COLUMNS)}; cannot infer the layout."
            )
        logger.warning(
            "Revolut CSV %s has no header row; assuming the standard column order %s.",
            path,
            ", ".join(STANDARD_COLUMNS),
        )
        header = list(STANDARD_COLUMNS)
        pending = [first]

    missing = REQUIRED_COLUMNS - set(header)
    if missing:
        raise RevolutParseError(
            f"{path}: missing required column(s) {', '.join(sorted(missing))}. "
            f"Found: {', '.join(header)}"
        )

    records: list[tuple[int, dict[str, str]]] = []
    line_no = first_line_no - 1
    for cells in [*pending, *reader]:
        line_no += 1
        if not any(c.strip() for c in cells):
            continue
        if len(cells) < len(header):
            cells = cells + [""] * (len(header) - len(cells))
        records.append((line_no, dict(zip(header, cells))))

    try:
        decimal_comma = detect_decimal_comma(
            [r.get(col, "") for _, r in records for col in (COL_QUANTITY, COL_PRICE, COL_TOTAL)]
        )
    except RevolutParseError as exc:
        raise RevolutParseError(f"{path}: {exc}") from exc

    rows: list[RevolutRow] = []
    for line_no, record in records:
        try:
            rows.append(_row_from_record(record, str(path), line_no, decimal_comma))
        except RevolutParseError as exc:
            raise RevolutParseError(f"{path} line {line_no}: {exc}") from exc

    if not rows:
        raise RevolutParseError(f"Revolut CSV file {path} is empty")
    return rows


def _row_from_record(
    record: dict[str, str], source: str, line: int, decimal_comma: bool
) -> RevolutRow:
    currency = record.get(COL_CURRENCY, "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise RevolutParseError(f"Invalid currency {record.get(COL_CURRENCY)!r}")
    total = parse_amount(record.get(COL_TOTAL, ""), currency, decimal_comma)
    if total is None:
        raise RevolutParseError("Missing 'Total Amount'")
    ticker = record.get(COL_TICKER, "").strip() or None
    return RevolutRow(
        source=source,
        line=line,
        timestamp=parse_timestamp(record.get(COL_DATE, "")),
        ticker=ticker,
        type=re.sub(r"\s+", " ", record.get(COL_TYPE, "").strip()).upper(),
        quantity=parse_decimal(record.get(COL_QUANTITY, ""), decimal_comma),
        price_per_share=parse_amount(record.get(COL_PRICE, ""), currency, decimal_comma),
        total_amount=total,
        currency=currency,
        fx_rate=_parse_fx_rate(record.get(COL_FX_RATE, "")),
    )


def _parse_fx_rate(value: str) -> Decimal | None:
    # Informational only; a rate is never thousands-grouped, so a lone comma
    # is always a decimal comma here.
    raw = value.strip()
    if not raw:
        return None
    try:
        return Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None
