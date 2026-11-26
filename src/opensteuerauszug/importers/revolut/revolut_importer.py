"""Revolut Invest importer.

Converts the Revolut Invest *account statement* CSV into a TaxStatement,
using the same accumulator / post-processing helpers as the other importers.

Key design choices
------------------
* The export is a transaction log with no holdings snapshot, so positions
  are rebuilt by replaying every row from the start of the account. The
  export must therefore cover *All time*; a sale that would take a holding
  below zero stops the import with a hint to re-export.
* Holdings with no activity during the tax year still appear, because the
  opening balance is carried in from earlier years.
* Timestamps are UTC and are converted to Swiss local time before the date
  is taken (see :func:`csv_parser.to_swiss_local`).
* Revolut gives tickers only. The ticker is the position's ``symbol``; an
  ISIN can be supplied per ticker via ``isin_map`` in the account settings
  (or later via the security identifiers CSV in the calculate phase).
* ``DIVIDEND`` rows are the cash received *after* foreign withholding tax,
  and the export does not state that tax. The importer books dividends
  gross with the withholding as its own payment, estimating the gross from
  a configurable rate (15% for US issuers by default, matching the W-8BEN
  treaty rate Revolut applies). Estimates are flagged as critical warnings.
  If a ``DIVIDEND TAX (CORRECTION)`` row that is not cancelled by an
  opposite one belongs to the dividend, that row is the tax and the
  dividend row is taken as already gross.
* Cash per currency is the running sum of all cash-moving rows; each
  currency becomes its own bank account.
* Unknown ``Type`` values stop the import instead of being dropped.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from opensteuerauszug.config.models import RevolutAccountSettings
from opensteuerauszug.importers.common import (
    CashAccountEntry,
    PositionHints,
    SecurityNameRegistry,
    SecurityPositionData,
    augment_list_of_bank_accounts,
    augment_list_of_securities,
    build_client,
    build_security_payment,
    parse_swiss_canton,
    resolve_first_last_name,
)
from opensteuerauszug.model.critical_warning import CriticalWarning, CriticalWarningCategory
from opensteuerauszug.model.ech0196 import (
    BankAccountPayment,
    Institution,
    ISINType,
    SecurityCategory,
    SecurityStock,
    TaxStatement,
    ValorNumber,
)
from opensteuerauszug.model.position import SecurityPosition

from .bank_csv_parser import RevolutBankRow, is_revolut_bank_csv, load_revolut_bank_csv
from .csv_parser import RevolutParseError, RevolutRow, is_revolut_csv, load_revolut_csv

logger = logging.getLogger(__name__)

# Default dividend withholding rates by issuer country, applied to Revolut's
# net dividend rows to recover the gross amount. US: 15% treaty rate with a
# W-8BEN, which Revolut requires before US shares can be traded.
DEFAULT_WITHHOLDING_RATES: dict[str, Decimal] = {"US": Decimal("0.15")}

# Fallback issuer country when no ISIN is known for a ticker.
_COUNTRY_FROM_CURRENCY: dict[str, str] = {"USD": "US", "GBP": "GB", "CHF": "CH"}

# Quantities below this are treated as zero (fractional-share rounding).
_QTY_EPSILON = Decimal("0.000001")
_CENT = Decimal("0.01")
_ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_CASH_TOLERANCE = Decimal("0.05")
_PRICE_PRECISION = Decimal("0.000001")

# A correction is attached to a dividend of the same ticker paid up to this
# many days before or after it.
_CORRECTION_WINDOW_BEFORE = timedelta(days=30)
_CORRECTION_WINDOW_AFTER = timedelta(days=5)

_SOURCE = "RevolutImporter"


class RevolutRowKind(Enum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    DIVIDEND_TAX = "dividend_tax"
    STOCK_SPLIT = "stock_split"
    CUSTODY_FEE = "custody_fee"
    CASH_IN = "cash_in"
    CASH_OUT = "cash_out"
    INTERNAL_TRANSFER = "internal_transfer"
    UNKNOWN = "unknown"


def classify_row(row: RevolutRow) -> RevolutRowKind:
    """Map a row's ``Type`` to a :class:`RevolutRowKind`."""
    t = row.type
    if t.startswith("BUY"):
        return RevolutRowKind.BUY
    if t.startswith("SELL"):
        return RevolutRowKind.SELL
    if t == "DIVIDEND":
        return RevolutRowKind.DIVIDEND
    if t.startswith("DIVIDEND TAX"):
        return RevolutRowKind.DIVIDEND_TAX
    if t == "STOCK SPLIT":
        return RevolutRowKind.STOCK_SPLIT
    if t == "CUSTODY FEE":
        return RevolutRowKind.CUSTODY_FEE
    if t == "CASH TOP-UP":
        return RevolutRowKind.CASH_IN
    if t == "CASH WITHDRAWAL":
        return RevolutRowKind.CASH_OUT
    if t.startswith("TRANSFER FROM REVOLUT"):
        return RevolutRowKind.INTERNAL_TRANSFER
    return RevolutRowKind.UNKNOWN


def merge_rows(files_rows: Sequence[list[RevolutRow]]) -> list[RevolutRow]:
    """Merge rows from several exports, dropping rows repeated across files.

    Overlapping exports repeat the same rows. A row identical to one in
    another file is kept only as many times as the file with the most
    copies has it, so genuine same-second duplicates within one file
    survive. The result is in chronological order (stable for ties).
    """
    if len(files_rows) == 1:
        merged = list(files_rows[0])
    else:
        max_counts: Counter = Counter()
        for rows in files_rows:
            for key, count in Counter(r.dedup_key for r in rows).items():
                max_counts[key] = max(max_counts[key], count)
        emitted: Counter = Counter()
        merged = []
        for rows in files_rows:
            for r in rows:
                if emitted[r.dedup_key] < max_counts[r.dedup_key]:
                    emitted[r.dedup_key] += 1
                    merged.append(r)
    return sorted(merged, key=lambda r: r.timestamp)


def _bank_row_key(r: RevolutBankRow) -> tuple:
    return (
        r.product,
        r.currency,
        r.type,
        r.started,
        r.completed,
        r.description,
        r.amount,
        r.fee,
        r.state,
        r.balance,
    )


def merge_bank_rows(files_rows: Sequence[list[RevolutBankRow]]) -> list[RevolutBankRow]:
    """Merge personal account statements like :func:`merge_rows` does."""
    max_counts: Counter = Counter()
    for rows in files_rows:
        for key, count in Counter(_bank_row_key(r) for r in rows).items():
            max_counts[key] = max(max_counts[key], count)
    emitted: Counter = Counter()
    merged = []
    for rows in files_rows:
        for r in rows:
            key = _bank_row_key(r)
            if emitted[key] < max_counts[key]:
                emitted[key] += 1
                merged.append(r)
    return merged


@dataclass
class PersonalAccountBalance:
    """Year-end state of one Revolut personal account (product + currency)."""

    product: str
    currency: str
    balance: Decimal
    reported_balance: Decimal | None
    active_in_period: bool
    first_activity: date | None
    last_activity: date | None


def personal_account_balances(
    rows: Sequence[RevolutBankRow], period_from: date, period_to: date
) -> list[PersonalAccountBalance]:
    """Balance of each (product, currency) account at the end of *period_to*.

    The balance is the running total of completed rows (amount minus fee),
    anchored on the balance the export reports before its first row, so a
    partial export still gives the right result. Revolut's own ``Balance``
    column is kept for cross-checking: a few rows (cashback, fee refunds)
    carry a balance snapshot out of order, so it is not used directly.
    """
    by_account: dict[tuple[str, str], list[RevolutBankRow]] = defaultdict(list)
    for r in rows:
        if r.is_completed:
            by_account[(r.product, r.currency)].append(r)

    result = []
    for (product, currency), account_rows in sorted(by_account.items()):
        account_rows.sort(key=lambda r: (r.completed, r.started))
        first = account_rows[0]
        running = (first.balance - first.net_amount) if first.balance is not None else Decimal(0)
        reported: Decimal | None = None
        active = False
        last_date: date | None = None
        for r in account_rows:
            assert r.completed is not None
            d = r.completed.date()
            if d > period_to:
                break
            running += r.net_amount
            if r.balance is not None:
                reported = r.balance
            last_date = d
            if d >= period_from:
                active = True
        if last_date is None:
            continue  # account opened after the period
        result.append(
            PersonalAccountBalance(
                product=product,
                currency=currency,
                balance=running,
                reported_balance=reported,
                active_in_period=active,
                first_activity=first.completed.date() if first.completed else None,
                last_activity=last_date,
            )
        )
    return result


@dataclass
class _Dividend:
    row: RevolutRow
    corrections: list[RevolutRow] = field(default_factory=list)


class RevolutImporter:
    """Import Revolut Invest account statement CSV(s) for a tax period."""

    def __init__(
        self,
        period_from: date,
        period_to: date,
        account_settings_list: list[RevolutAccountSettings],
        identifier_map: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """``identifier_map`` is the loaded security identifiers CSV
        (``symbol -> {"isin": ..., "valor": ...}``); ``isin_map`` in the
        account settings takes precedence over it."""
        self.period_from = period_from
        self.period_to = period_to
        self.account_settings_list = account_settings_list
        self.settings: RevolutAccountSettings | None = (
            account_settings_list[0] if account_settings_list else None
        )
        if len(account_settings_list) > 1:
            logger.warning(
                "Several Revolut accounts are configured; using '%s'. Process each "
                "account's export separately with --broker/override if needed.",
                account_settings_list[0].account_name_alias,
            )
        self._depot_id = self.settings.account_number if self.settings else "REVOLUT"
        self._isin_map: dict[str, str] = {
            k.upper(): v.strip().upper()
            for k, v in (self.settings.isin_map if self.settings else {}).items()
        }
        for ticker, isin in self._isin_map.items():
            if not _ISIN_RE.fullmatch(isin):
                raise ValueError(f"Invalid ISIN {isin!r} for {ticker} in Revolut isin_map")
        self._valor_map: dict[str, int] = {}
        for symbol, ids in (identifier_map or {}).items():
            key = symbol.strip().upper()
            isin = (ids.get("isin") or "").strip().upper()
            if isin and key not in self._isin_map:
                if _ISIN_RE.fullmatch(isin):
                    self._isin_map[key] = isin
                else:
                    logger.warning("Ignoring invalid ISIN %r for %s in identifiers CSV", isin, key)
            if ids.get("valor"):
                self._valor_map[key] = int(ids["valor"])
        self._rates: dict[str, Decimal] = dict(DEFAULT_WITHHOLDING_RATES)
        if self.settings:
            for key, rate in self.settings.dividend_withholding_rates.items():
                self._rates[key.upper()] = Decimal(str(rate))
        self._warnings: list[CriticalWarning] = []
        self._currency_hint: dict[SecurityPosition, str] = {}

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def import_path(self, path: str | Path) -> TaxStatement:
        """Import a single CSV file or every Revolut CSV in a directory."""
        p = Path(path)
        if p.is_dir():
            files = sorted(f for f in p.iterdir() if f.suffix.lower() == ".csv" and f.is_file())
            if not any(is_revolut_csv(f) or is_revolut_bank_csv(f) for f in files):
                raise FileNotFoundError(f"No Revolut statement CSV found in {p}")
            return self.import_files(files)
        if not p.is_file():
            raise FileNotFoundError(f"Revolut input not found: {p}")
        return self.import_files([p])

    def import_files(self, paths: Iterable[str | Path]) -> TaxStatement:
        """Import explicit CSV files and return a TaxStatement.

        Each file may be an Invest account statement or a personal account
        statement; the kind is recognised from its header.
        """
        invest_rows: list[list[RevolutRow]] = []
        bank_rows: list[list[RevolutBankRow]] = []
        for path in map(Path, paths):
            if is_revolut_bank_csv(path):
                bank_rows.append(load_revolut_bank_csv(path))
            elif is_revolut_csv(path):
                invest_rows.append(load_revolut_csv(path))
            else:
                if path.suffix.lower() == ".csv":
                    logger.info("Ignoring %s: not a Revolut statement", path)
                    continue
                raise RevolutParseError(f"{path} is not a Revolut statement CSV")
        if not invest_rows and not bank_rows:
            raise RevolutParseError("No Revolut statement CSV among the inputs")
        rows = merge_rows(invest_rows) if invest_rows else []
        return self.import_rows(rows, bank_rows=merge_bank_rows(bank_rows))

    def import_rows(
        self,
        rows: Sequence[RevolutRow],
        bank_rows: Sequence[RevolutBankRow] = (),
    ) -> TaxStatement:
        """Build the TaxStatement from already-parsed rows."""
        self._warnings = []
        self._currency_hint = {}
        end_plus_one = self.period_to + timedelta(days=1)

        name_registry = SecurityNameRegistry()
        positions: dict[SecurityPosition, SecurityPositionData] = defaultdict(
            lambda: SecurityPositionData({"stocks": [], "payments": []})
        )
        currency_of = self._currency_hint
        running_qty: dict[SecurityPosition, Decimal] = defaultdict(Decimal)
        opening_qty: dict[SecurityPosition, Decimal] = {}
        opening_taken = False

        cash: dict[str, Decimal] = defaultdict(Decimal)
        cash_active_in_period: set[str] = set()

        dividends: list[_Dividend] = []
        corrections: list[RevolutRow] = []

        if rows and rows[0].local_date > self.period_from:
            logger.info(
                "Revolut export starts on %s, after the period start %s. This is fine "
                "only if the account was opened then; otherwise re-export with "
                "Period 'All time'.",
                rows[0].local_date,
                self.period_from,
            )

        for row in rows:
            d = row.local_date
            if d > self.period_to:
                continue
            in_period = d >= self.period_from
            kind = classify_row(row)

            if kind == RevolutRowKind.UNKNOWN:
                raise NotImplementedError(
                    f"Unknown Revolut transaction type {row.type!r} ({row.source} line "
                    f"{row.line}). The row may be tax-relevant; please report it so "
                    "support can be added."
                )
            if kind == RevolutRowKind.INTERNAL_TRANSFER:
                # Moves cash or holdings between Revolut entities; the history of
                # the previous entity is already part of the same export.
                logger.debug("Skipping internal transfer row %s:%d", row.source, row.line)
                continue

            # Opening balances are snapshotted before the first in-period row.
            if in_period and not opening_taken:
                opening_qty.update(running_qty)
                opening_taken = True

            if kind in (RevolutRowKind.BUY, RevolutRowKind.SELL, RevolutRowKind.STOCK_SPLIT):
                sec_pos = self._sec_pos_for(row)
                currency_of.setdefault(sec_pos, row.currency)
                qty, unit_price, cash_delta, label = self._trade_values(row, kind)
                running_qty[sec_pos] += qty
                if running_qty[sec_pos] < -_QTY_EPSILON:
                    raise ValueError(
                        f"Revolut holding of {row.ticker} would become negative "
                        f"({running_qty[sec_pos]}) on {d} ({row.source} line {row.line}). "
                        "The export is probably incomplete: export the account statement "
                        "with Period 'All time'."
                    )
                cash[row.currency] += cash_delta
                if in_period:
                    positions[sec_pos]["stocks"].append(
                        SecurityStock(
                            referenceDate=d,
                            mutation=True,
                            quantity=qty,
                            unitPrice=unit_price,
                            balanceCurrency=row.currency,
                            quotationType="PIECE",
                            name=label,
                        )
                    )
                    if cash_delta:
                        cash_active_in_period.add(row.currency)
            elif kind == RevolutRowKind.DIVIDEND:
                self._require_ticker(row)
                cash[row.currency] += row.total_amount
                if in_period:
                    cash_active_in_period.add(row.currency)
                    dividends.append(_Dividend(row))
            elif kind == RevolutRowKind.DIVIDEND_TAX:
                self._require_ticker(row)
                cash[row.currency] += row.total_amount
                if in_period:
                    cash_active_in_period.add(row.currency)
                    corrections.append(row)
            elif kind == RevolutRowKind.CUSTODY_FEE:
                cash[row.currency] += -abs(row.total_amount)
                if in_period:
                    cash_active_in_period.add(row.currency)
            elif kind == RevolutRowKind.CASH_IN:
                cash[row.currency] += abs(row.total_amount)
                if in_period:
                    cash_active_in_period.add(row.currency)
            elif kind == RevolutRowKind.CASH_OUT:
                cash[row.currency] += -abs(row.total_amount)
                if in_period:
                    cash_active_in_period.add(row.currency)

        if not opening_taken:
            # No row fell inside the period: everything held now was held at start.
            opening_qty.update(running_qty)

        # Holdings carried into the period (including ones never traded in it).
        all_positions = set(running_qty) | set(positions)
        for sec_pos in all_positions:
            currency = currency_of[sec_pos]
            opening = _clean_qty(opening_qty.get(sec_pos, Decimal(0)))
            closing = _clean_qty(running_qty.get(sec_pos, Decimal(0)))
            data = positions[sec_pos]
            if opening != 0:
                data["stocks"].insert(
                    0,
                    SecurityStock(
                        referenceDate=self.period_from,
                        mutation=False,
                        quantity=opening,
                        balanceCurrency=currency,
                        quotationType="PIECE",
                    ),
                )
            data["stocks"].append(
                SecurityStock(
                    referenceDate=end_plus_one,
                    mutation=False,
                    quantity=closing,
                    balanceCurrency=currency,
                    quotationType="PIECE",
                )
            )

        self._book_dividends(dividends, corrections, positions, name_registry, currency_of)

        # Drop positions with nothing to report in the period.
        for sec_pos in list(positions):
            data = positions[sec_pos]
            has_mutation = any(s.mutation for s in data["stocks"])
            has_balance = any(not s.mutation and s.quantity != 0 for s in data["stocks"])
            if not (has_mutation or has_balance or data["payments"]):
                del positions[sec_pos]

        unmapped = sorted({p.symbol for p in positions if p.isin is None})
        if unmapped:
            logger.warning(
                "No ISIN known for Revolut ticker(s) %s. Add them to the security "
                "identifiers CSV (symbol,isin,valor) or to isin_map in the account "
                "settings so the Kursliste can value them.",
                ", ".join(unmapped),
            )

        statement = TaxStatement(
            minorVersion=1,
            periodFrom=self.period_from,
            periodTo=self.period_to,
            taxPeriod=self.period_from.year,
            listOfSecurities=None,
            listOfBankAccounts=None,
        )
        statement.institution = Institution(name="Revolut")
        self._apply_client(statement)

        augment_list_of_securities(
            statement,
            positions,
            name_registry=name_registry,
            hints_for=self._hints_for,
            strict_consistency=False,
            assume_zero_if_no_balances=True,
        )

        cash_entries: list[CashAccountEntry] = []
        if rows and (self.settings is None or self.settings.include_cash):
            cash_entries.extend(self._cash_entries(cash, cash_active_in_period))
        cash_entries.extend(self._personal_account_entries(bank_rows))
        augment_list_of_bank_accounts(statement, cash_entries)

        statement.critical_warnings.extend(self._warnings)
        return statement

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_ticker(self, row: RevolutRow) -> str:
        if not row.ticker:
            raise RevolutParseError(
                f"{row.source} line {row.line}: {row.type} row without a ticker"
            )
        return row.ticker

    def _sec_pos_for(self, row: RevolutRow) -> SecurityPosition:
        ticker = self._require_ticker(row)
        return self._sec_pos_for_ticker(ticker)

    def _sec_pos_for_ticker(self, ticker: str) -> SecurityPosition:
        isin = self._isin_map.get(ticker.upper())
        valor = self._valor_map.get(ticker.upper())
        return SecurityPosition(
            depot=self._depot_id,
            symbol=ticker,
            isin=ISINType(isin) if isin else None,
            valor=ValorNumber(valor) if valor else None,
            description=ticker,
        )

    @staticmethod
    def _trade_values(
        row: RevolutRow, kind: RevolutRowKind
    ) -> tuple[Decimal, Decimal | None, Decimal, str]:
        """Return (signed quantity, unit price, cash delta, label) for a stock row."""
        if row.quantity is None:
            raise RevolutParseError(f"{row.source} line {row.line}: {row.type} without quantity")
        if kind == RevolutRowKind.STOCK_SPLIT:
            # The quantity is the number of shares added (negative for a reverse
            # split); the total is zero and no cash moves.
            if row.total_amount != 0:
                logger.warning(
                    "STOCK SPLIT row %s:%d has a non-zero total %s; ignoring the amount.",
                    row.source,
                    row.line,
                    row.total_amount,
                )
            return row.quantity, None, Decimal(0), "Stock split"
        quantity = abs(row.quantity)
        if quantity == 0:
            raise RevolutParseError(f"{row.source} line {row.line}: {row.type} with zero quantity")
        total = abs(row.total_amount)
        # Price from the total, not the rounded "Price per share" column.
        unit_price = (total / quantity).quantize(_PRICE_PRECISION, rounding=ROUND_HALF_UP)
        if kind == RevolutRowKind.BUY:
            return quantity, unit_price, -total, "Buy"
        return -quantity, unit_price, total, "Sell"

    def _country_for(self, ticker: str, currency: str | None) -> str | None:
        """Issuer country from the configured ISIN, else guessed from the currency."""
        isin = self._isin_map.get(ticker.upper())
        if isin and not isin.startswith("X"):
            return isin[:2]
        if currency:
            return _COUNTRY_FROM_CURRENCY.get(currency)
        return None

    def _rate_for(self, ticker: str, currency: str) -> tuple[Decimal | None, str]:
        """Return (withholding rate, where it came from) for a dividend."""
        if ticker.upper() in self._rates:
            return self._rates[ticker.upper()], f"configured for {ticker}"
        country = self._country_for(ticker, currency)
        if country is None:
            return None, "an unknown issuer country (no ISIN configured)"
        if country in self._rates:
            source = "configured" if self._is_configured(country) else "default"
            return self._rates[country], f"{source} for issuer country {country}"
        return None, f"issuer country {country}"

    def _is_configured(self, key: str) -> bool:
        return bool(self.settings) and key in {
            k.upper() for k in self.settings.dividend_withholding_rates
        }

    def _hints_for(self, sec_pos: SecurityPosition) -> PositionHints:
        isin = sec_pos.isin or ""
        category: SecurityCategory = "FUND" if isin[:2] in ("IE", "LU") else "SHARE"
        return PositionHints(
            security_category=category,
            country=self._country_for(sec_pos.symbol, self._currency_hint.get(sec_pos)) or "US",
        )

    def _book_dividends(
        self,
        dividends: list[_Dividend],
        corrections: list[RevolutRow],
        positions: dict[SecurityPosition, SecurityPositionData],
        name_registry: SecurityNameRegistry,
        currency_of: dict[SecurityPosition, str],
    ) -> None:
        # Corrections usually come in cancelling +/- pairs within a second.
        # Only the net per (ticker, date, currency) matters.
        net_corrections: dict[tuple[str, date, str], Decimal] = defaultdict(Decimal)
        first_row: dict[tuple[str, date, str], RevolutRow] = {}
        for c in corrections:
            key = (c.ticker or "", c.local_date, c.currency)
            net_corrections[key] += c.total_amount
            first_row.setdefault(key, c)

        unattached: list[tuple[RevolutRow, Decimal]] = []
        for key, amount in net_corrections.items():
            if amount == 0:
                continue
            ticker, d, currency = key
            same_day = [
                dv
                for dv in dividends
                if dv.row.ticker == ticker
                and dv.row.currency == currency
                and dv.row.local_date == d
            ]
            candidates = same_day or [
                dv
                for dv in dividends
                if dv.row.ticker == ticker
                and dv.row.currency == currency
                and d - _CORRECTION_WINDOW_BEFORE
                <= dv.row.local_date
                <= d + _CORRECTION_WINDOW_AFTER
            ]
            if len(candidates) == 1:
                candidates[0].corrections.append(_with_amount(first_row[key], amount))
            else:
                unattached.append((first_row[key], amount))

        for dv in dividends:
            row = dv.row
            ticker = row.ticker or ""
            sec_pos = self._sec_pos_for_ticker(ticker)
            currency_of.setdefault(sec_pos, row.currency)
            name_registry.update(sec_pos, ticker, 1)
            data = positions[sec_pos]
            if not any(not s.mutation for s in data["stocks"]):
                # Dividend for a holding that no trade row explains.
                logger.warning(
                    "Revolut dividend for %s on %s but no holding of it in the export.",
                    ticker,
                    row.local_date,
                )
            net = row.total_amount

            if dv.corrections:
                tax = sum((c.total_amount for c in dv.corrections), Decimal(0))
                data["payments"].append(self._payment(row, net, "Dividend"))
                data["payments"].append(
                    self._payment(row, tax, "Dividend Tax (Correction)", is_withholding=True)
                )
                self._warn(
                    f"Revolut {ticker} dividend of {net} {row.currency} on {row.local_date} "
                    f"has a separate tax correction of {tax} {row.currency}. The dividend row "
                    "was taken as the gross amount and the correction as its withholding tax. "
                    "Check both against the dividend details in the Revolut app.",
                    ticker,
                    row.local_date,
                )
                continue

            rate, rate_source = self._rate_for(ticker, row.currency)
            if rate is None:
                data["payments"].append(self._payment(row, net, "Dividend (net)"))
                self._warn(
                    f"Revolut {ticker} dividend of {net} {row.currency} on {row.local_date} "
                    "is booked as received (after foreign tax) because no withholding rate "
                    f"is known for {rate_source}. The gross dividend is understated and "
                    "the withholding tax is missing. Set dividend_withholding_rates for "
                    f"{ticker} in the Revolut account configuration, and consider "
                    "--use-broker-withholding off so the Kursliste withholding is not "
                    "capped to zero.",
                    ticker,
                    row.local_date,
                )
                continue

            if not (Decimal(0) <= rate < Decimal(1)):
                raise ValueError(f"Invalid dividend withholding rate {rate} for {ticker}")
            gross = (net / (Decimal(1) - rate)).quantize(_CENT, rounding=ROUND_HALF_UP)
            tax = gross - net
            data["payments"].append(self._payment(row, gross, "Dividend"))
            if tax != 0:
                data["payments"].append(
                    self._payment(row, -tax, "Withholding Tax (estimated)", is_withholding=True)
                )
                self._warn(
                    f"Revolut {ticker} dividend on {row.local_date}: received {net} "
                    f"{row.currency}; booked gross {gross} with withholding {tax} using a "
                    f"{_percent(rate)}% rate ({rate_source}). Revolut's export does not state "
                    "the tax; check it in the app's dividend details.",
                    ticker,
                    row.local_date,
                )

        for row, amount in unattached:
            ticker = row.ticker or ""
            sec_pos = self._sec_pos_for_ticker(ticker)
            currency_of.setdefault(sec_pos, row.currency)
            positions[sec_pos]["payments"].append(
                self._payment(row, amount, "Dividend Tax (Correction)", is_withholding=True)
            )
            self._warn(
                f"Revolut {ticker} dividend tax correction of {amount} {row.currency} on "
                f"{row.local_date} could not be matched to a single dividend; it was booked "
                "as a standalone withholding entry. Check it against your Revolut records.",
                ticker,
                row.local_date,
            )

    @staticmethod
    def _payment(row: RevolutRow, amount: Decimal, label: str, is_withholding: bool = False):
        return build_security_payment(
            payment_date=row.local_date,
            description=row.ticker or "",
            currency=row.currency,
            amount=amount,
            broker_label=label,
            is_withholding=is_withholding,
        )

    def _warn(self, message: str, identifier: str | None, payment_date: date | None):
        logger.warning(message)
        self._warnings.append(
            CriticalWarning(
                category=CriticalWarningCategory.OTHER,
                message=message,
                source=_SOURCE,
                identifier=identifier,
                payment_date=payment_date,
            )
        )

    def _apply_client(self, statement: TaxStatement) -> None:
        settings = self.settings
        if not settings:
            return
        first_name, last_name = resolve_first_last_name(full_name=settings.full_name)
        canton = parse_swiss_canton(settings.canton)
        if canton:
            statement.canton = canton
        client = build_client(
            client_number=settings.account_number, first_name=first_name, last_name=last_name
        )
        if client is not None:
            statement.client = [client]

    def _personal_account_entries(
        self, bank_rows: Sequence[RevolutBankRow]
    ) -> list[CashAccountEntry]:
        """Bank accounts for the Revolut personal account and its pockets."""
        if not bank_rows:
            return []
        country = self.settings.bank_country if self.settings else "LT"
        interest, interest_tax = self._personal_account_interest(bank_rows)
        entries: list[CashAccountEntry] = []
        for acc in personal_account_balances(bank_rows, self.period_from, self.period_to):
            key = (acc.product, acc.currency)
            if acc.balance == 0 and not acc.active_in_period and not interest.get(key):
                continue
            label = f"{self._depot_id} {acc.product} {acc.currency}"
            tax = interest_tax.get(key)
            if tax:
                self._warn(
                    f"Revolut {acc.product} {acc.currency}: tax of {-tax} {acc.currency} was "
                    f"withheld on interest during {self.period_from.year}. The interest is "
                    "reported gross; claim the foreign tax yourself if it is recoverable.",
                    label,
                    None,
                )
            if acc.reported_balance is not None and abs(acc.reported_balance - acc.balance) > _CENT:
                self._warn(
                    f"Revolut {acc.product} {acc.currency}: the balance at {self.period_to} "
                    f"computed from the transactions ({acc.balance}) differs from the last "
                    f"balance in the export ({acc.reported_balance}). The computed value was "
                    "used; check it against the year-end statement in the Revolut app.",
                    label,
                    None,
                )
            balance = acc.balance
            if balance < 0:
                self._warn(
                    f"Revolut {acc.product} {acc.currency} balance at {self.period_to} is "
                    f"negative ({balance}).",
                    label,
                    None,
                )
            entries.append(
                CashAccountEntry(
                    account_id=self._depot_id,
                    currency=acc.currency,
                    closing_balance=balance,
                    country=country,
                    name=label,
                    number=f"{self._depot_id}-{acc.product.upper().replace(' ', '')}-{acc.currency}",
                    payments=interest.get(key, []),
                )
            )
        return entries

    def _personal_account_interest(
        self, bank_rows: Sequence[RevolutBankRow]
    ) -> tuple[dict[tuple[str, str], list[BankAccountPayment]], dict[tuple[str, str], Decimal]]:
        """Interest payments and interest tax per (product, currency) in the period.

        Interest is booked gross: when Revolut lists the tax on interest as
        separate rows, those rows are summed and reported in a warning instead
        of being deducted from the income.
        """
        payments: dict[tuple[str, str], list[BankAccountPayment]] = defaultdict(list)
        tax: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        for r in bank_rows:
            if not r.is_completed or r.completed is None:
                continue
            d = r.completed.date()
            if not (self.period_from <= d <= self.period_to):
                continue
            key = (r.product, r.currency)
            if r.is_interest_tax:
                tax[key] += r.net_amount
            elif r.is_interest:
                payments[key].append(
                    BankAccountPayment(
                        paymentDate=d,
                        name=(r.description or r.type or "Interest")[:200],
                        amountCurrency=r.currency,
                        amount=r.amount,
                    )
                )
        return dict(payments), dict(tax)

    def _cash_entries(
        self, cash: dict[str, Decimal], active_in_period: set[str]
    ) -> list[CashAccountEntry]:
        country = self.settings.bank_country if self.settings else "LT"
        entries: list[CashAccountEntry] = []
        for currency in sorted(cash):
            balance = cash[currency]
            if balance == 0 and currency not in active_in_period:
                continue
            if -_CASH_TOLERANCE <= balance < 0:
                # Revolut's own rounding can leave a cent or so below zero.
                logger.info("Revolut %s cash balance %s rounded to 0", currency, balance)
                balance = Decimal(0)
            elif balance < 0:
                self._warn(
                    f"Revolut {currency} cash balance reconstructed from the export is "
                    f"negative ({balance}) at {self.period_to}. Some cash movements are "
                    "probably missing (e.g. currency exchanges outside the investment "
                    "account); the balance was set to 0. Check it against your Revolut "
                    "statement.",
                    f"{self._depot_id}-{currency}",
                    None,
                )
                balance = Decimal(0)
            entries.append(
                CashAccountEntry(
                    account_id=self._depot_id,
                    currency=currency,
                    closing_balance=balance,
                    payments=[],
                    country=country,
                    name=f"{self._depot_id} {currency}",
                    number=f"{self._depot_id}-{currency}",
                )
            )
        return entries


def _clean_qty(q: Decimal) -> Decimal:
    return Decimal(0) if abs(q) < _QTY_EPSILON else q


def _with_amount(row: RevolutRow, amount: Decimal) -> RevolutRow:
    return replace(row, total_amount=amount)


def _percent(rate: Decimal) -> str:
    text = f"{rate * 100:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text
