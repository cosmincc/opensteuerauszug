"""Tests for Revolut personal account (bank) balances, on synthetic statements only."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from opensteuerauszug.importers.revolut.bank_csv_parser import (
    is_revolut_bank_csv,
    load_revolut_bank_csv,
)
from opensteuerauszug.importers.revolut.csv_parser import RevolutParseError
from opensteuerauszug.importers.revolut.revolut_importer import (
    RevolutImporter,
    personal_account_balances,
)

HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"

# fmt: off
STATEMENT = [
    "Topup,Current,2023-05-01 10:00:00,2023-05-01 10:00:05,Top-up by card,1000.00,0.00,EUR,COMPLETED,1000.00",
    "Card Payment,Current,2023-12-30 18:00:00,2023-12-31 09:00:00,Shop,-100.00,0.00,EUR,COMPLETED,900.00",
    # Started in 2023 but completed in 2024: counts for 2024.
    "Card Payment,Current,2023-12-31 23:00:00,2024-01-02 09:00:00,Shop,-50.00,0.00,EUR,COMPLETED,850.00",
    "Card Payment,Current,2024-02-01 12:00:00,,Shop,-20.00,0.00,EUR,REVERTED,",
    "Transfer,Savings,2024-03-01 12:00:00,2024-03-01 12:00:01,To EUR Spare change,0.40,0.00,EUR,COMPLETED,0.40",
    "Exchange,Current,2024-04-01 12:00:00,2024-04-01 12:00:01,Exchanged to CHF,-200.00,0.00,EUR,COMPLETED,649.60",
    "Exchange,Current,2024-04-01 12:00:00,2024-04-01 12:00:01,Exchanged from EUR,190.00,0.00,CHF,COMPLETED,190.00",
    "Fee,Current,2024-05-01 00:00:00,2024-05-01 00:00:00,Premium plan fee,0.00,7.99,CHF,COMPLETED,182.01",
    # Cashback carrying a balance snapshot out of order, as seen in real exports.
    "CASHBACK,Current,2024-06-01 08:00:00,2024-06-01 08:00:00,Cashback,1.00,0.00,EUR,COMPLETED,500.00",
    "Card Payment,Current,2024-06-02 08:00:00,2024-06-02 08:00:00,Shop,-10.00,0.00,EUR,COMPLETED,640.60",
    "Card Payment,Current,2024-12-20 08:00:00,,Shop,-5.00,0.00,EUR,PENDING,",
    "Topup,Current,2025-01-05 08:00:00,2025-01-05 08:00:00,Top-up,300.00,0.00,EUR,COMPLETED,940.60",
    "Topup,Current,2019-01-05 08:00:00,2019-01-05 08:00:00,Top-up,10.00,0.00,GBP,COMPLETED,10.00",
    "Card Payment,Current,2019-02-05 08:00:00,2019-02-05 08:00:00,Shop,-10.00,0.00,GBP,COMPLETED,0.00",
]
# fmt: on

PERIOD_FROM = date(2024, 1, 1)
PERIOD_TO = date(2024, 12, 31)


def _write(tmp_path: Path, rows, name="account-statement.csv") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _balances(tmp_path, rows=STATEMENT):
    parsed = load_revolut_bank_csv(_write(tmp_path, rows))
    return {
        (a.product, a.currency): a
        for a in personal_account_balances(parsed, PERIOD_FROM, PERIOD_TO)
    }


def test_statement_rows_are_parsed_with_state_fee_and_balance(tmp_path):
    rows = load_revolut_bank_csv(_write(tmp_path, STATEMENT))

    fee = rows[7]
    assert fee.type == "Fee"
    assert fee.fee == Decimal("7.99")
    assert fee.net_amount == Decimal("-7.99")
    reverted = rows[3]
    assert reverted.state == "REVERTED"
    assert reverted.balance is None
    assert not reverted.is_completed


def test_personal_account_statement_is_told_apart_from_invest_statement(tmp_path):
    bank = _write(tmp_path, STATEMENT)
    invest = tmp_path / "invest.csv"
    invest.write_text("Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate\n")

    assert is_revolut_bank_csv(bank)
    assert not is_revolut_bank_csv(invest)


def test_year_end_balance_counts_completed_rows_by_completion_date(tmp_path):
    balances = _balances(tmp_path)

    # 1000 - 100 - 50 (completed 2024) - 200 + 1 - 10; reverted/pending rows ignored.
    assert balances[("Current", "EUR")].balance == Decimal("641.00")
    assert balances[("Current", "CHF")].balance == Decimal("182.01")
    assert balances[("Savings", "EUR")].balance == Decimal("0.40")


def test_out_of_order_balance_snapshot_does_not_change_the_result(tmp_path):
    eur = _balances(tmp_path)[("Current", "EUR")]

    # Revolut's last 2024 row says 640.60, which ignores the 1.00 cashback;
    # the computed balance includes it and the difference is visible.
    assert eur.reported_balance == Decimal("640.60")
    assert eur.balance == Decimal("641.00")


def test_partial_export_is_anchored_on_the_first_reported_balance(tmp_path):
    rows = [
        "Card Payment,Current,2024-02-01 10:00:00,2024-02-01 10:00:00,Shop,-20.00,0.00,USD,COMPLETED,480.00",
        "Topup,Current,2024-03-01 10:00:00,2024-03-01 10:00:00,Top-up,20.00,0.00,USD,COMPLETED,500.00",
    ]

    assert _balances(tmp_path, rows)[("Current", "USD")].balance == Decimal("500.00")


def test_account_opened_after_the_period_is_not_reported(tmp_path):
    rows = [
        "Topup,Current,2025-03-01 10:00:00,2025-03-01 10:00:00,Top-up,5.00,0.00,THB,COMPLETED,5.00"
    ]

    assert _balances(tmp_path, rows) == {}


def test_importer_reports_one_bank_account_per_product_and_currency(tmp_path):
    statement = RevolutImporter(PERIOD_FROM, PERIOD_TO, []).import_path(_write(tmp_path, STATEMENT))

    accounts = {
        a.bankAccountNumber: a.taxValue.balance for a in statement.listOfBankAccounts.bankAccount
    }
    # GBP is empty and unused in 2024, so it is left out.
    assert accounts == {
        "REVOLUT-CURRENT-CHF": Decimal("182.01"),
        "REVOLUT-CURRENT-EUR": Decimal("641.00"),
        "REVOLUT-SAVINGS-EUR": Decimal("0.40"),
    }
    assert statement.listOfSecurities is None
    (eur,) = [
        a
        for a in statement.listOfBankAccounts.bankAccount
        if a.bankAccountCurrency == "EUR" and "CURRENT" in a.bankAccountNumber
    ]
    assert eur.bankAccountCountry == "LT"
    assert eur.bankAccountName == "REVOLUT Current EUR"
    # The mismatch with Revolut's own last balance is flagged for review.
    assert any("640.60" in w.message for w in statement.critical_warnings)


def test_directory_with_invest_and_personal_statements_reports_both(tmp_path):
    folder = tmp_path / "revolut"
    folder.mkdir()
    _write(folder, STATEMENT)
    (folder / "invest.csv").write_text(
        "Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate\n"
        "2024-02-01T09:00:00Z,,CASH TOP-UP,,,USD 200,USD,1\n"
        "2024-02-01T14:30:00Z,AAPL,BUY - MARKET,1,USD 150,USD 150,USD,1\n",
        encoding="utf-8",
    )

    statement = RevolutImporter(PERIOD_FROM, PERIOD_TO, []).import_path(folder)

    numbers = {a.bankAccountNumber for a in statement.listOfBankAccounts.bankAccount}
    assert numbers == {
        "REVOLUT-USD",
        "REVOLUT-CURRENT-CHF",
        "REVOLUT-CURRENT-EUR",
        "REVOLUT-SAVINGS-EUR",
    }
    assert statement.listOfSecurities is not None


def test_overlapping_personal_statements_are_not_double_counted(tmp_path):
    folder = tmp_path / "revolut"
    folder.mkdir()
    _write(folder, STATEMENT[:8], "a.csv")
    _write(folder, STATEMENT[4:], "b.csv")

    statement = RevolutImporter(PERIOD_FROM, PERIOD_TO, []).import_path(folder)

    eur = next(
        a
        for a in statement.listOfBankAccounts.bankAccount
        if a.bankAccountNumber == "REVOLUT-CURRENT-EUR"
    )
    assert eur.taxValue.balance == Decimal("641.00")


def test_invalid_amount_reports_the_line(tmp_path):
    rows = [
        "Topup,Current,2024-03-01 10:00:00,2024-03-01 10:00:00,Top-up,abc,0.00,USD,COMPLETED,5.00"
    ]

    with pytest.raises(RevolutParseError, match="line 2"):
        load_revolut_bank_csv(_write(tmp_path, rows))


# fmt: off
INTEREST_ROWS = [
    "Topup,Savings,2024-01-02 10:00:00,2024-01-02 10:00:00,To EUR Savings,1000.00,0.00,EUR,COMPLETED,1000.00",
    "Interest,Savings,2024-01-31 23:00:00,2024-02-01 00:30:00,Interest earned,2.10,0.00,EUR,COMPLETED,1002.10",
    "Gross Interest,Savings,2024-02-29 23:00:00,2024-03-01 00:30:00,Gross interest,2.00,0.00,EUR,COMPLETED,1004.10",
    "TAX,Savings,2024-02-29 23:00:00,2024-03-01 00:30:01,Tax withheld on interest,-0.30,0.00,EUR,COMPLETED,1003.80",
    "Interest,Savings,2023-12-31 23:00:00,2023-12-31 23:30:00,Interest earned,1.00,0.00,EUR,COMPLETED,1004.80",
    "Fee,Current,2024-03-01 00:00:00,2024-03-01 00:00:00,Interest Bearing Account fee,0.00,1.00,EUR,COMPLETED,99.00",
]
# fmt: on


def test_savings_interest_in_the_year_is_booked_gross_on_its_account(tmp_path):
    statement = RevolutImporter(PERIOD_FROM, PERIOD_TO, []).import_path(
        _write(tmp_path, INTEREST_ROWS)
    )

    accounts = {a.bankAccountNumber: a for a in statement.listOfBankAccounts.bankAccount}
    savings = accounts["REVOLUT-SAVINGS-EUR"]
    assert [(p.paymentDate, p.amount, p.name) for p in savings.payment] == [
        (date(2024, 2, 1), Decimal("2.10"), "Interest earned"),
        (date(2024, 3, 1), Decimal("2.00"), "Gross interest"),
    ]
    assert all(p.amountCurrency == "EUR" for p in savings.payment)
    # The fee mentioning interest is not income.
    assert accounts["REVOLUT-CURRENT-EUR"].payment == []
    # Tax on interest is not netted off the income but reported for review.
    assert any("tax of 0.30 EUR" in w.message for w in statement.critical_warnings)
