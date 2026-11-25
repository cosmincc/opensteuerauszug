"""Parser for Trading212 transaction CSV exports."""

import csv
import logging
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence, Union

from ._models import (
    T212CashTransaction,
    T212Dividend,
    T212Order,
    normalize_currency,
    parse_t212_date,
)

logger = logging.getLogger(__name__)

BUY_ACTIONS = {"Market buy", "Limit buy", "Stop buy"}
SELL_ACTIONS = {"Market sell", "Limit sell", "Stop sell", "Stop limit sell"}
SPLIT_OPEN_ACTIONS = {"Stock split open"}
SPLIT_CLOSE_ACTIONS = {"Stock split close"}
# T212 exports dividends with optional sub-type suffixes, e.g.:
#   "Dividend (Ordinary)", "Dividend (Dividends paid by us corporations)", "Dividend (Bonus)"
# Match by prefix so new sub-types are handled automatically.
DIVIDEND_ACTION_PREFIX = "Dividend"
CASH_INCOME_ACTIONS = {"Interest on cash", "Lending interest"}
IGNORED_ACTIONS = {
    "Deposit",
    "Withdrawal",
    "Currency conversion",
    "Market order",
    "Spending cashback",
    # Cash adjustments (WHT refunds, interest reversals) — no ticker, not attributable
    "Result adjustment",
}

# Map from T212 CSV column header → internal field name
COLUMN_ALIASES: dict[str, str] = {
    "Action": "action",
    "Time": "time",
    "ISIN": "isin",
    "Ticker": "ticker",
    "Name": "name",
    "No. of shares": "quantity",
    "Price / share": "price",
    "Currency (Price / share)": "price_currency",
    "Exchange rate": "fx_rate",
    "Total": "total",
    "Currency (Total)": "total_currency",
    "Withholding tax": "withholding_tax",
    "Currency (Withholding tax)": "withholding_tax_currency",
    "Result": "result",
    "Currency (Result)": "result_currency",
    "Notes": "notes",
}

REQUIRED_COLUMNS = {"action", "time"}


class T212CsvParser:
    """
    Parses one or more Trading212 transaction history CSV exports.

    Each CSV is expected to contain at least 'Action' and 'Time' columns.
    When several files are given (e.g. one export per year), rows are merged
    and exact duplicates (same T212 ``ID``, or identical content for rows
    without an ID) are dropped so overlapping export ranges are harmless.

    Unknown actions are skipped and recorded in ``unknown_actions`` so the
    importer can surface them as critical warnings.
    """

    def __init__(self, csv_path: Union[str, Sequence[str]]):
        self._paths: List[str] = [csv_path] if isinstance(csv_path, str) else list(csv_path)
        self._path = ", ".join(self._paths)
        self.unknown_actions: Counter[str] = Counter()
        self.earliest_date: Optional[date] = None
        self.latest_date: Optional[date] = None

    def parse(self) -> tuple[list[T212Order], list[T212Dividend], list[T212CashTransaction]]:
        """Parse the CSV(s) and return (orders, dividends, cash_transactions)."""
        orders: list[T212Order] = []
        dividends: list[T212Dividend] = []
        cash_transactions: list[T212CashTransaction] = []
        seen_rows: set = set()

        for path in self._paths:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV file '{path}' appears to be empty.")

                col_map = _build_column_map(reader.fieldnames)
                _validate_required_columns(col_map, path)

                for row_num, raw_row in enumerate(reader, start=2):
                    row = {col_map[k]: v for k, v in raw_row.items() if k in col_map}
                    action = (row.get("action") or "").strip()
                    if not action:
                        continue

                    dedup_key = _row_dedup_key(raw_row)
                    if dedup_key in seen_rows:
                        logger.debug("CSV row %d (%s): duplicate row, skipping", row_num, path)
                        continue
                    seen_rows.add(dedup_key)
                    self._track_date(row.get("time"))

                    if action in BUY_ACTIONS or action in SELL_ACTIONS:
                        order = _parse_order(action, row, row_num, path)
                        if order is not None:
                            orders.append(order)
                    elif action in SPLIT_OPEN_ACTIONS or action in SPLIT_CLOSE_ACTIONS:
                        order = _parse_split(action, row, row_num, path)
                        if order is not None:
                            orders.append(order)
                    elif action.startswith(DIVIDEND_ACTION_PREFIX):
                        div = _parse_dividend(action, row, row_num, path)
                        if div is not None:
                            dividends.append(div)
                    elif action in CASH_INCOME_ACTIONS:
                        tx = _parse_cash_transaction(action, row, row_num, path)
                        if tx is not None:
                            cash_transactions.append(tx)
                    elif action not in IGNORED_ACTIONS:
                        logger.warning(
                            "CSV row %d (%s): unsupported action '%s', skipping",
                            row_num,
                            path,
                            action,
                        )
                        self.unknown_actions[action] += 1

        logger.info(
            "Parsed T212 CSV '%s': %d orders, %d dividends, %d cash transactions",
            self._path,
            len(orders),
            len(dividends),
            len(cash_transactions),
        )
        return orders, dividends, cash_transactions

    def _track_date(self, value: Optional[str]) -> None:
        try:
            d = parse_t212_date(value or "")
        except ValueError:
            return
        if self.earliest_date is None or d < self.earliest_date:
            self.earliest_date = d
        if self.latest_date is None or d > self.latest_date:
            self.latest_date = d


def _row_dedup_key(raw_row: dict) -> tuple:
    """Key identifying a CSV row across (possibly overlapping) exports."""
    row_id = (raw_row.get("ID") or "").strip()
    if row_id:
        return ("id", (raw_row.get("Action") or "").strip(), row_id)
    return ("row",) + tuple(sorted((k, (v or "").strip()) for k, v in raw_row.items() if k))


def _build_column_map(fieldnames: Sequence[str]) -> dict[str, str]:
    """Map raw CSV headers to internal field names."""
    result = {}
    for raw_header in fieldnames:
        if raw_header in COLUMN_ALIASES:
            result[raw_header] = COLUMN_ALIASES[raw_header]
    return result


def _validate_required_columns(col_map: dict[str, str], path: str) -> None:
    present = set(col_map.values())
    missing = REQUIRED_COLUMNS - present
    if missing:
        raise ValueError(
            f"CSV file '{path}' is missing required columns: {missing}. "
            f"Found columns map to: {present}"
        )


def _parse_decimal(value: Optional[str]) -> Optional[Decimal]:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace(",", ""))
    except InvalidOperation:
        return None


def _parse_order(action: str, row: dict, row_num: int, path: str) -> Optional[T212Order]:
    side = "BUY" if action in BUY_ACTIONS else "SELL"
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker, skipping", row_num, path)
        return None
    try:
        filled_at = parse_t212_date(row.get("time", ""))
        qty = _parse_decimal(row.get("quantity"))
        price = _parse_decimal(row.get("price"))
        if qty is None or price is None:
            logger.warning(
                "CSV row %d (%s): missing quantity or price for %s, skipping", row_num, path, ticker
            )
            return None
        total_raw = _parse_decimal(row.get("total"))
        total = total_raw if total_raw is not None else Decimal("0")
        price_norm, currency = normalize_currency(price, row.get("price_currency") or "")
        price = price_norm if price_norm is not None else price
        return T212Order(
            filled_at=filled_at,
            side=side,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            quantity=qty,
            price=price,
            currency=currency,
            fx_rate=_parse_decimal(row.get("fx_rate")),
            total_account_currency=total,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning(
            "CSV row %d (%s): error parsing order for %s: %s", row_num, path, ticker, exc
        )
        return None


def _parse_dividend(action: str, row: dict, row_num: int, path: str) -> Optional[T212Dividend]:
    """Parse a dividend row.

    T212 semantics (verified against real exports):
      * ``No. of shares``      – shares entitled to the dividend
      * ``Price / share``      – *gross* dividend per share, in
        ``Currency (Price / share)`` (the instrument currency)
      * ``Withholding tax``    – tax withheld, in ``Currency (Withholding tax)``
      * ``Total``              – *net* amount credited, in ``Currency (Total)``
        (the account currency)
    """
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker for dividend, skipping", row_num, path)
        return None
    try:
        paid_on = parse_t212_date(row.get("time", ""))
        amount_raw = _parse_decimal(row.get("total"))
        amount = amount_raw if amount_raw is not None else Decimal("0")
        price_ccy_raw = (row.get("price_currency") or "").strip()
        gross_per_share, instrument_currency = normalize_currency(
            _parse_decimal(row.get("price")), price_ccy_raw
        )
        wht_raw = _parse_decimal(row.get("withholding_tax"))
        wht_ccy_raw = (row.get("withholding_tax_currency") or "").strip() or price_ccy_raw
        wht, wht_currency = normalize_currency(
            abs(wht_raw) if wht_raw is not None else None, wht_ccy_raw
        )
        account_currency = (row.get("total_currency") or "").strip().upper()
        return T212Dividend(
            paid_on=paid_on,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            amount_account_currency=amount,
            gross_per_share=gross_per_share,
            withholding_tax=wht if wht else None,
            instrument_currency=instrument_currency or account_currency,
            fx_rate=_parse_decimal(row.get("fx_rate")),
            quantity=_parse_decimal(row.get("quantity")),
            account_currency=account_currency,
            withholding_currency=wht_currency or instrument_currency,
            action=action,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning(
            "CSV row %d (%s): error parsing dividend for %s: %s", row_num, path, ticker, exc
        )
        return None


def _parse_split(action: str, row: dict, row_num: int, path: str) -> Optional[T212Order]:
    """Parse a stock-split row as a quantity-only mutation (no unit price)."""
    side = "BUY" if action in SPLIT_OPEN_ACTIONS else "SELL"
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        logger.warning("CSV row %d (%s): missing ticker for stock split, skipping", row_num, path)
        return None
    try:
        filled_at = parse_t212_date(row.get("time", ""))
        qty = _parse_decimal(row.get("quantity"))
        if qty is None:
            logger.warning(
                "CSV row %d (%s): missing quantity for stock split %s, skipping",
                row_num,
                path,
                ticker,
            )
            return None
        return T212Order(
            filled_at=filled_at,
            side=side,
            ticker=ticker,
            name=(row.get("name") or ticker).strip(),
            isin=(row.get("isin") or "").strip() or None,
            quantity=qty,
            price=Decimal("0"),  # No meaningful unit price for a split
            currency=normalize_currency(None, row.get("price_currency") or "")[1],
            fx_rate=None,
            total_account_currency=Decimal("0"),
            is_split=True,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning(
            "CSV row %d (%s): error parsing stock split for %s: %s", row_num, path, ticker, exc
        )
        return None


def _parse_cash_transaction(
    action: str, row: dict, row_num: int, path: str
) -> Optional[T212CashTransaction]:
    """Parse an interest-on-cash or lending-interest row."""
    try:
        tx_date = parse_t212_date(row.get("time", ""))
        amount = _parse_decimal(row.get("total"))
        currency = (row.get("total_currency") or "").strip()
        if amount is None or not currency:
            logger.warning(
                "CSV row %d (%s): missing amount or currency for '%s', skipping",
                row_num,
                path,
                action,
            )
            return None
        return T212CashTransaction(
            transaction_date=tx_date,
            action=action,
            amount=amount,
            currency=currency,
        )
    except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
        logger.warning("CSV row %d (%s): error parsing cash transaction: %s", row_num, path, exc)
        return None
