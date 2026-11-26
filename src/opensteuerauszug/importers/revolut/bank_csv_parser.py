"""Parser for the Revolut personal account statement CSV.

This is the statement of the Revolut current account and its pockets
(Savings, etc.), exported from the app under the account's
*Statement* → *Excel/CSV*. The layout is::

    Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
    Card Payment,Current,2024-03-01 12:00:00,2024-03-02 08:00:00,Shop,-12.50,0.00,EUR,COMPLETED,987.50

``Balance`` is the running balance of the (Product, Currency) account after
the row. Rows that are ``REVERTED`` or still ``PENDING`` carry no balance and
never moved money. Only completed rows are relevant for balances.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .csv_parser import RevolutParseError, detect_decimal_comma, parse_decimal

logger = logging.getLogger(__name__)

COL_TYPE = "Type"
COL_PRODUCT = "Product"
COL_STARTED = "Started Date"
COL_COMPLETED = "Completed Date"
COL_DESCRIPTION = "Description"
COL_AMOUNT = "Amount"
COL_FEE = "Fee"
COL_CURRENCY = "Currency"
COL_STATE = "State"
COL_BALANCE = "Balance"

REQUIRED_COLUMNS = {
    COL_PRODUCT,
    COL_STARTED,
    COL_COMPLETED,
    COL_AMOUNT,
    COL_CURRENCY,
    COL_STATE,
    COL_BALANCE,
}

# Interest rows: Type "Interest" / "Gross Interest" / "Net Interest" (seen in
# other Revolut parsers), or a description such as "Interest earned".
_INTEREST_TYPE_RE = re.compile(r"(?i)(gross |net )?interest( paid| earned)?")
_INTEREST_DESCRIPTION_RE = re.compile(
    r"(?i)\s*(gross |net )?interest( paid| earned| payment)?(\s+(for|on|from)\b.*)?\s*$"
)
_NON_INTEREST_TYPES = {"CARD PAYMENT", "CARD REFUND", "EXCHANGE", "ATM", "TAX", "TAX_REFUND", "FEE"}

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S")


@dataclass(frozen=True)
class RevolutBankRow:
    """One row of the personal account statement."""

    source: str
    line: int
    type: str
    product: str
    started: datetime
    completed: datetime | None
    description: str
    amount: Decimal
    fee: Decimal
    currency: str
    state: str
    balance: Decimal | None

    @property
    def is_completed(self) -> bool:
        return self.state == "COMPLETED" and self.completed is not None

    @property
    def is_interest(self) -> bool:
        """Interest credited to the account (savings / deposit interest)."""
        return _INTEREST_TYPE_RE.fullmatch(self.type.strip()) is not None or (
            _INTEREST_DESCRIPTION_RE.match(self.description) is not None
            and self.type.upper() not in _NON_INTEREST_TYPES
        )

    @property
    def is_interest_tax(self) -> bool:
        """Tax withheld on (or refunded for) interest."""
        text = f"{self.type} {self.description}".upper()
        return "TAX" in text and ("INTEREST" in text or "WITHHELD" in text)

    @property
    def net_amount(self) -> Decimal:
        """Change of the account balance caused by the row."""
        return self.amount - self.fee


def _parse_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)  # noqa: DTZ007 - export is in local time
        except ValueError:
            continue
    raise RevolutParseError(f"Unrecognised date format: {value!r}")


def _read_header(path: Path) -> tuple[str, list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        first = fh.readline()
    counts = {d: first.count(d) for d in (",", ";", "\t")}
    delimiter = max(counts, key=lambda d: counts[d])
    cells = next(csv.reader([first], delimiter=delimiter), [])
    return delimiter, [c.strip() for c in cells]


def is_revolut_bank_csv(path: Path) -> bool:
    """Return True if *path* has a Revolut personal account statement header."""
    try:
        _, header = _read_header(path)
    except (OSError, UnicodeDecodeError):
        return False
    return REQUIRED_COLUMNS.issubset(header)


def load_revolut_bank_csv(path: str | Path) -> list[RevolutBankRow]:
    """Read a Revolut personal account statement CSV, in file order."""
    path = Path(path)
    delimiter, header = _read_header(path)
    missing = REQUIRED_COLUMNS - set(header)
    if missing:
        raise RevolutParseError(
            f"{path}: not a Revolut account statement; missing column(s) "
            f"{', '.join(sorted(missing))}"
        )
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    reader.fieldnames = [c.strip() for c in reader.fieldnames or []]
    records = [
        (i, r) for i, r in enumerate(reader, start=2) if any((v or "").strip() for v in r.values())
    ]
    if not records:
        raise RevolutParseError(f"Revolut account statement {path} is empty")
    decimal_comma = detect_decimal_comma(
        [r.get(c) or "" for _, r in records for c in (COL_AMOUNT, COL_FEE, COL_BALANCE)]
    )

    rows: list[RevolutBankRow] = []
    for line, r in records:
        try:
            currency = (r.get(COL_CURRENCY) or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise RevolutParseError(f"Invalid currency {r.get(COL_CURRENCY)!r}")
            started = _parse_datetime(r.get(COL_STARTED) or "")
            if started is None:
                raise RevolutParseError("Missing 'Started Date'")
            amount = parse_decimal(r.get(COL_AMOUNT) or "", decimal_comma)
            if amount is None:
                raise RevolutParseError("Missing 'Amount'")
            rows.append(
                RevolutBankRow(
                    source=str(path),
                    line=line,
                    type=(r.get(COL_TYPE) or "").strip(),
                    product=(r.get(COL_PRODUCT) or "").strip() or "Current",
                    started=started,
                    completed=_parse_datetime(r.get(COL_COMPLETED) or ""),
                    description=(r.get(COL_DESCRIPTION) or "").strip(),
                    amount=amount,
                    fee=parse_decimal(r.get(COL_FEE) or "", decimal_comma) or Decimal(0),
                    currency=currency,
                    state=(r.get(COL_STATE) or "").strip().upper(),
                    balance=parse_decimal(r.get(COL_BALANCE) or "", decimal_comma),
                )
            )
        except RevolutParseError as exc:
            raise RevolutParseError(f"{path} line {line}: {exc}") from exc
    return rows
