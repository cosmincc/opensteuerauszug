"""Tests for the Revolut importer, built on synthetic account statements only."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.config.models import RevolutAccountSettings
from opensteuerauszug.importers.revolut.revolut_importer import RevolutImporter
from opensteuerauszug.model.ech0196 import Security, TaxStatement

HEADER = "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate"

PERIOD_FROM = date(2024, 1, 1)
PERIOD_TO = date(2024, 12, 31)

# A small but complete synthetic history, starting before the tax year.
HISTORY = [
    "2023-02-01T09:00:00.000Z,,CASH TOP-UP,,,USD 5000,USD,1.0",
    "2023-02-01T14:30:00.123456Z,AAPL,BUY - MARKET,10,USD 150.00,USD 1500,USD,1.0",
    "2023-02-02T14:30:00.123456Z,MSFT,BUY - LIMIT,4,USD 250.00,USD 1000,USD,1.0",
    "2023-06-01T03:00:00.000Z,,CUSTODY FEE,,,USD -0.10,USD,1.0",
    # In the tax year
    "2024-02-15T13:00:00.000Z,AAPL,DIVIDEND,,,USD 2.04,USD,1.0",
    "2024-05-10T15:00:00.000Z,AAPL,SELL - MARKET,4,USD 180.00,USD 720.00,USD,1.0",
    "2024-06-10T08:00:00.000Z,AAPL,STOCK SPLIT,6,,USD 0,USD,1.0",
    "2024-09-01T10:00:00.000Z,,CASH WITHDRAWAL,,,USD -1000,USD,1.0",
    # 23:30 UTC on 31 December is 1 January in Zurich: next tax year.
    "2024-12-31T23:30:00.000Z,AAPL,SELL - MARKET,1,USD 100,USD 100,USD,1.0",
]


def _write(tmp_path: Path, rows: list[str], name: str = "revolut.csv") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _settings(**kwargs) -> RevolutAccountSettings:
    base = {
        "account_number": "RV-1",
        "broker_name": "revolut",
        "account_name_alias": "main",
        "full_name": "Max Muster",
        "canton": "ZH",
    }
    base.update(kwargs)
    return RevolutAccountSettings(**base)


def _import(
    tmp_path: Path, rows: list[str], settings: RevolutAccountSettings | None = None
) -> TaxStatement:
    path = _write(tmp_path, rows)
    importer = RevolutImporter(PERIOD_FROM, PERIOD_TO, [settings] if settings else [])
    return importer.import_path(path)


def _securities(statement: TaxStatement) -> dict[str, Security]:
    assert statement.listOfSecurities is not None
    return {s.symbol: s for d in statement.listOfSecurities.depot for s in d.security}


def _balance(security: Security, on: date) -> Decimal:
    (stock,) = [s for s in security.stock if not s.mutation and s.referenceDate == on]
    return stock.quantity


def _cash(statement: TaxStatement) -> dict[str, Decimal]:
    assert statement.listOfBankAccounts is not None
    return {
        a.bankAccountCurrency: a.taxValue.balance for a in statement.listOfBankAccounts.bankAccount
    }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def test_positions_are_rebuilt_from_the_full_history(tmp_path):
    securities = _securities(_import(tmp_path, HISTORY))

    aapl = securities["AAPL"]
    assert _balance(aapl, PERIOD_FROM) == Decimal(10)
    # 10 - 4 sold + 6 from the split; the New Year's Eve sale belongs to 2025.
    assert _balance(aapl, date(2025, 1, 1)) == Decimal(12)
    mutations = [(s.referenceDate, s.quantity, s.name) for s in aapl.stock if s.mutation]
    assert mutations == [
        (date(2024, 5, 10), Decimal(-4), "Sell"),
        (date(2024, 6, 10), Decimal(6), "Stock split"),
    ]


def test_holding_without_trades_in_the_tax_year_is_still_reported(tmp_path):
    securities = _securities(_import(tmp_path, HISTORY))

    msft = securities["MSFT"]
    assert _balance(msft, PERIOD_FROM) == Decimal(4)
    assert _balance(msft, date(2025, 1, 1)) == Decimal(4)
    assert not any(s.mutation for s in msft.stock)


def test_trade_price_is_taken_from_the_total_not_the_rounded_price_column(tmp_path):
    rows = [
        "2024-03-01T14:00:00Z,,CASH TOP-UP,,,USD 1000,USD,1",
        "2024-03-01T14:30:00Z,NVDA,BUY - MARKET,3,USD 33.33,USD 100,USD,1",
    ]

    (stock,) = [s for s in _securities(_import(tmp_path, rows))["NVDA"].stock if s.mutation]

    assert stock.unitPrice == Decimal("33.333333")
    assert stock.balanceCurrency == "USD"


def test_position_closed_before_the_tax_year_is_omitted(tmp_path):
    rows = [
        "2022-03-01T14:00:00Z,,CASH TOP-UP,,,USD 1000,USD,1",
        "2022-03-01T14:30:00Z,TSLA,BUY - MARKET,1,USD 200,USD 200,USD,1",
        "2022-09-01T14:30:00Z,TSLA,SELL - MARKET,1,USD 250,USD 250,USD,1",
        "2023-03-01T14:30:00Z,AAPL,BUY - MARKET,1,USD 150,USD 150,USD,1",
    ]

    securities = _securities(_import(tmp_path, rows))

    assert set(securities) == {"AAPL"}


def test_sale_beyond_known_holdings_asks_for_an_all_time_export(tmp_path):
    rows = ["2024-05-10T15:00:00Z,AAPL,SELL - MARKET,4,USD 180,USD 720,USD,1"]

    with pytest.raises(ValueError, match="All time"):
        _import(tmp_path, rows)


def test_internal_transfers_between_revolut_entities_are_ignored(tmp_path):
    rows = HISTORY + [
        (
            "2024-03-22T10:48:36Z,,TRANSFER FROM REVOLUT BANK UAB TO REVOLUT SECURITIES "
            "EUROPE UAB,,,USD 205.48,USD,1"
        ),
        (
            "2024-03-22T10:48:36Z,AAPL,TRANSFER FROM REVOLUT TRADING LTD TO REVOLUT "
            "SECURITIES EUROPE UAB,10,,USD 0,USD,1"
        ),
    ]

    statement = _import(tmp_path, rows)

    assert _balance(_securities(statement)["AAPL"], date(2025, 1, 1)) == Decimal(12)
    assert _cash(statement) == _cash(_import(tmp_path, HISTORY))


def test_unknown_transaction_type_stops_the_import(tmp_path):
    rows = HISTORY + ["2024-04-01T10:00:00Z,AAPL,SPINOFF,1,,USD 0,USD,1"]

    with pytest.raises(NotImplementedError, match="SPINOFF"):
        _import(tmp_path, rows)


def test_isin_from_settings_is_attached_and_sets_country_and_category(tmp_path):
    rows = [
        "2024-03-01T14:00:00Z,,CASH TOP-UP,,,EUR 1000,EUR,1",
        "2024-03-01T14:30:00Z,VUSA,BUY - MARKET,2,EUR 90,EUR 180,EUR,1",
    ]
    settings = _settings(isin_map={"VUSA": "IE00B3XXRP09"})

    vusa = _securities(_import(tmp_path, rows, settings))["VUSA"]

    assert vusa.isin == "IE00B3XXRP09"
    assert vusa.country == "IE"
    assert vusa.securityCategory == "FUND"


def test_invalid_isin_in_settings_is_rejected():
    with pytest.raises(ValueError, match="Invalid ISIN"):
        RevolutImporter(PERIOD_FROM, PERIOD_TO, [_settings(isin_map={"AAPL": "nope"})])


# ---------------------------------------------------------------------------
# Dividends
# ---------------------------------------------------------------------------


def _payments(security: Security):
    return [
        (p.paymentDate, p.amount, p.nonRecoverableTaxAmountOriginal, p.broker_label_original)
        for p in security.payment
    ]


def test_us_dividend_is_booked_gross_with_estimated_withholding(tmp_path):
    statement = _import(tmp_path, HISTORY)

    # 2.04 received at 15% withholding -> 2.40 gross, 0.36 tax.
    assert _payments(_securities(statement)["AAPL"]) == [
        (date(2024, 2, 15), Decimal("2.40"), None, "Dividend"),
        (date(2024, 2, 15), Decimal("-0.36"), Decimal("0.36"), "Withholding Tax (estimated)"),
    ]
    assert any("15% rate" in w.message for w in statement.critical_warnings)


def test_configured_withholding_rate_overrides_the_default(tmp_path):
    settings = _settings(dividend_withholding_rates={"AAPL": Decimal("0.30")})

    payments = _payments(_securities(_import(tmp_path, HISTORY, settings))["AAPL"])

    # 2.04 / 0.70 = 2.914... -> 2.91 gross, 0.87 tax.
    assert payments[0][1] == Decimal("2.91")
    assert payments[1][2] == Decimal("0.87")


def test_zero_withholding_rate_books_the_dividend_as_received(tmp_path):
    settings = _settings(dividend_withholding_rates={"US": Decimal(0)})

    payments = _payments(_securities(_import(tmp_path, HISTORY, settings))["AAPL"])

    assert payments == [(date(2024, 2, 15), Decimal("2.04"), None, "Dividend")]


def test_dividend_with_unknown_withholding_rate_is_booked_net_with_a_warning(tmp_path):
    rows = [
        "2023-03-01T14:00:00Z,,CASH TOP-UP,,,EUR 1000,EUR,1",
        "2023-03-01T14:30:00Z,ASML,BUY - MARKET,1,EUR 600,EUR 600,EUR,1",
        "2024-05-01T10:00:00Z,ASML,DIVIDEND,,,EUR 1.27,EUR,1",
    ]

    statement = _import(tmp_path, rows)

    assert _payments(_securities(statement)["ASML"]) == [
        (date(2024, 5, 1), Decimal("1.27"), None, "Dividend (net)")
    ]
    assert any(
        "ASML" in w.message and "dividend_withholding_rates" in w.message
        for w in statement.critical_warnings
    )


def test_cancelling_tax_correction_pair_changes_nothing(tmp_path):
    rows = HISTORY + [
        "2024-02-15T13:00:01Z,AAPL,DIVIDEND TAX (CORRECTION),,,USD -0.36,USD,1",
        "2024-02-15T13:00:02Z,AAPL,DIVIDEND TAX (CORRECTION),,,USD 0.36,USD,1",
    ]

    assert _payments(_securities(_import(tmp_path, rows))["AAPL"]) == _payments(
        _securities(_import(tmp_path, HISTORY))["AAPL"]
    )


def test_lone_tax_correction_is_the_withholding_of_a_dividend_paid_in_full(tmp_path):
    rows = [r for r in HISTORY if "DIVIDEND" not in r] + [
        "2024-02-15T13:00:00Z,AAPL,DIVIDEND,,,USD 2.40,USD,1",
        "2024-02-15T13:28:00Z,AAPL,DIVIDEND TAX (CORRECTION),,,USD -0.36,USD,1",
    ]

    statement = _import(tmp_path, rows)

    assert _payments(_securities(statement)["AAPL"]) == [
        (date(2024, 2, 15), Decimal("2.40"), None, "Dividend"),
        (date(2024, 2, 15), Decimal("-0.36"), Decimal("0.36"), "Dividend Tax (Correction)"),
    ]
    # Cash reflects what actually moved: 2.40 in, 0.36 out.
    assert _cash(statement)["USD"] == _cash(_import(tmp_path, HISTORY))["USD"]


def test_dividend_paid_after_new_year_in_zurich_belongs_to_next_year(tmp_path):
    rows = HISTORY + ["2024-12-31T23:10:00Z,MSFT,DIVIDEND,,,USD 2.55,USD,1"]

    assert _securities(_import(tmp_path, rows))["MSFT"].payment == []


# ---------------------------------------------------------------------------
# Cash and statement
# ---------------------------------------------------------------------------


def test_cash_balance_is_the_sum_of_cash_movements_up_to_year_end(tmp_path):
    statement = _import(tmp_path, HISTORY, _settings())

    # 5000 - 1500 - 1000 - 0.10 + 2.04 + 720 - 1000 (2025 sale excluded)
    assert _cash(statement) == {"USD": Decimal("2221.94")}
    (account,) = statement.listOfBankAccounts.bankAccount
    assert account.bankAccountNumber == "RV-1-USD"
    assert account.bankAccountCountry == "LT"


def test_negative_reconstructed_cash_is_reported_as_zero_with_a_warning(tmp_path):
    rows = [
        "2024-03-01T14:00:00Z,,CASH TOP-UP,,,EUR 1000,EUR,1",
        "2024-03-01T14:30:00Z,AAPL,BUY - MARKET,1,USD 150,USD 150,USD,1",
    ]

    statement = _import(tmp_path, rows)

    assert _cash(statement) == {"EUR": Decimal(1000), "USD": Decimal(0)}
    assert any("USD cash balance" in w.message for w in statement.critical_warnings)


def test_cash_accounts_can_be_disabled(tmp_path):
    statement = _import(tmp_path, HISTORY, _settings(include_cash=False))

    assert statement.listOfBankAccounts is None


def test_statement_carries_institution_client_and_canton(tmp_path):
    statement = _import(tmp_path, HISTORY, _settings())

    assert statement.institution.name == "Revolut"
    assert statement.canton == "ZH"
    assert statement.client[0].clientNumber == "RV-1"
    assert statement.client[0].lastName == "Muster"
    assert statement.periodFrom == PERIOD_FROM
    assert statement.taxPeriod == 2024


def test_overlapping_exports_in_a_directory_are_merged_without_duplicates(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    _write(export_dir, HISTORY[:6], "2023.csv")
    _write(export_dir, HISTORY[3:], "2024.csv")
    (export_dir / "identifiers.csv").write_text("symbol,isin,valor\n", encoding="utf-8")

    merged = RevolutImporter(PERIOD_FROM, PERIOD_TO, []).import_path(export_dir)

    single = _import(tmp_path, HISTORY)
    assert _cash(merged) == _cash(single)
    assert _balance(_securities(merged)["AAPL"], date(2025, 1, 1)) == Decimal(12)


def test_same_second_duplicates_within_one_export_are_kept(tmp_path):
    rows = [
        "2024-03-01T14:00:00Z,,CASH TOP-UP,,,USD 1000,USD,1",
        "2024-03-01T14:30:00Z,AAPL,BUY - MARKET,1,USD 150,USD 150,USD,1",
        "2024-03-01T14:30:00Z,AAPL,BUY - MARKET,1,USD 150,USD 150,USD,1",
    ]

    aapl = _securities(_import(tmp_path, rows))["AAPL"]

    assert _balance(aapl, date(2025, 1, 1)) == Decimal(2)


def test_identifiers_csv_supplies_isin_and_valor_and_config_takes_precedence(tmp_path):
    identifier_map = {
        "AAPL": {"isin": "US0378331005", "valor": 908440},
        "MSFT": {"isin": "US5949181045", "valor": None},
    }
    settings = _settings(isin_map={"MSFT": "US5949181046"})
    path = _write(tmp_path, HISTORY)

    statement = RevolutImporter(
        PERIOD_FROM, PERIOD_TO, [settings], identifier_map=identifier_map
    ).import_path(path)

    securities = _securities(statement)
    assert securities["AAPL"].isin == "US0378331005"
    assert securities["AAPL"].valorNumber == 908440
    assert securities["MSFT"].isin == "US5949181046"
