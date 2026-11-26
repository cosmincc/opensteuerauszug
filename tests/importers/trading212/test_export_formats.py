"""Trading212 CSV export format variants and corporate-action handling.

The header layouts mirror the three export generations Trading212 has
produced (documented by KapJI/capital-gains-calculator's test exports):

* ~2020: ``Time``, GBP-only ``Total (GBP)`` / ``Result (GBP)`` columns,
  ``Not available`` exchange rates on dividends.
* ~2024: ``Time`` with milliseconds, ``Total`` + ``Currency (Total)``.
* 2026+: ``Time (UTC)`` and many fee / conversion columns.

All rows below are synthetic.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from opensteuerauszug.importers.trading212._models import parse_t212_date
from opensteuerauszug.importers.trading212.api_client import T212ApiClient
from opensteuerauszug.importers.trading212.csv_parser import T212CsvParser
from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer
from opensteuerauszug.model.critical_warning import CriticalWarningCategory

from .conftest import make_order, make_settings

HEADER_2020 = (
    "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,Currency (Price / share),"
    "Exchange rate,Result (GBP),Total (GBP),Withholding tax,Currency (Withholding tax),"
    "Charge amount (GBP),Transaction fee (GBP),Finra fee,Currency (Finra fee),Notes,ID"
)
HEADER_2024 = (
    "Action,Time,ISIN,Ticker,Name,No. of shares,Price / share,Currency (Price / share),"
    "Exchange rate,Result,Currency (Result),Total,Currency (Total),Withholding tax,"
    "Currency (Withholding tax),Transaction fee,Notes,ID,Currency conversion fee,"
    "Currency (Currency conversion fee),Currency (Transaction fee)"
)
HEADER_2026 = (
    "Action,Time (UTC),ISIN,Ticker,Name,Notes,ID,No. of shares,Price / share,"
    "Currency (Price / share),Exchange rate,Result,Currency (Result),Total,Currency (Total),"
    "Withholding tax,Currency (Withholding tax),Stamp duty reserve tax,"
    "Currency (Stamp duty reserve tax),French transaction tax,Currency (French transaction tax),"
    "Transaction fee,Finra fee,Currency conversion from amount,"
    "Currency (Currency conversion from amount),Currency conversion to amount,"
    "Currency (Currency conversion to amount),Currency conversion fee,"
    "Currency (Currency conversion fee),Currency (Transaction fee),Currency (Finra fee),"
    "Merchant name,Merchant category"
)

P_FROM = date(2024, 1, 1)
P_TO = date(2024, 12, 31)


def _write(path, header, *rows):
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _importer(**kw):
    kw.setdefault("account_currency", "GBP")
    kw.setdefault("cash_balances", {"GBP": Decimal("0")})
    return Trading212Importer(P_FROM, P_TO, [make_settings(**kw)])


def _balances(sec):
    return {s.name: s.quantity for s in sec.stock if not s.mutation}


class TestTimestamps:
    def test_utc_converted_to_swiss_date(self):
        # 23:30 UTC on 31 Dec is already 1 Jan in Zurich (CET = UTC+1)
        assert parse_t212_date("2024-12-31 23:30:00") == date(2025, 1, 1)
        assert parse_t212_date("2024-12-31 22:59:59.123") == date(2024, 12, 31)
        # Summer time (CEST = UTC+2)
        assert parse_t212_date("2024-06-30T22:30:00+00:00") == date(2024, 7, 1)
        assert parse_t212_date("2025-01-01T00:10:00Z") == date(2025, 1, 1)
        assert parse_t212_date("2024-05-01") == date(2024, 5, 1)

    def test_year_end_trade_falls_in_next_swiss_year(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2026,
            "Market buy,2024-12-31 23:30:00.000,US0000000200,ACME,Acme Corp,,id-1,"
            "10.0000000000,150.00,USD,1.25,,,1201.80,GBP,,,,,,,,,,,,,1.80,GBP,,,,",
        )
        stmt = _importer().import_from(str(f))
        # Bought on 1 Jan 2025 Swiss time: nothing held during 2024
        assert stmt.listOfSecurities is None


class TestExportGenerations:
    def test_2020_export_with_gbp_total_columns(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2020,
            "Market buy,2023-11-25 14:30:08,US0000000300,OLD,\"Old Co\",4.0000000000,"
            "10.00,USD,1.25000,,32.00,,,,,,GBP,,EOF1",
            "Dividend (Ordinary),2024-04-02 10:15:07,US0000000300,OLD,\"Old Co\",4.0000000000,"
            "0.17,USD,Not available,,0.54,0.12,USD,,,,GBP,,",
            "Interest on cash,2024-05-01 10:00:00,,,,,,,,,0.33,,,,,,,,int-1",
        )
        orders, divs, cash = T212CsvParser(str(f)).parse()
        assert orders[0].total_account_currency == Decimal("32.00")
        (d,) = divs
        assert d.amount_account_currency == Decimal("0.54")
        assert d.account_currency == "GBP"
        assert d.fx_rate is None  # "Not available"
        (c,) = cash
        assert (c.amount, c.currency) == (Decimal("0.33"), "GBP")

        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        div, wht = sec.payment
        # 4 x 0.17 net + 0.12 tax = 0.80 USD gross
        assert (div.amount, div.amountCurrency) == (Decimal("0.80"), "USD")
        assert div.amountPerUnit == Decimal("0.20")
        assert (wht.amountCurrency, wht.nonRecoverableTaxAmountOriginal) == ("USD", Decimal("0.12"))

    def test_2024_export_with_millisecond_times(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2024,
            "Market buy,2024-01-02 16:10:05.175,US0000000002,FOO,\"Foo Company\",24.0000000000,"
            "124.01,USD,1.25229,,\"GBP\",2381.35,\"GBP\",,,,,EOF9,4.71,\"GBP\",",
            "Stop limit buy,2024-02-02 16:10:05.175,US0000000002,FOO,\"Foo Company\",1.0000000000,"
            "120.00,USD,1.25229,,\"GBP\",96.00,\"GBP\",,,,,EOF10,,,",
            "Card debit,2024-02-03 10:00:00.000,,,,,,,,,,-12.00,\"GBP\",,,,,card-1,,,",
            "Result adjustment,2024-01-04 13:12:49.174,,,,,,,,,,0.75,\"GBP\",,,,"
            "\"Withholding tax refund\",adj-1,,,",
        )
        parser = T212CsvParser(str(f))
        orders, _, _ = parser.parse()
        assert [o.quantity for o in orders] == [Decimal("24"), Decimal("1")]
        assert not parser.unknown_actions

    def test_2026_export_with_time_utc_column(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2026,
            "Market buy,2024-05-02 14:30:05.123,US0000000200,ACME,Acme Corp,,id-2,"
            "10.0000000000,150.00,USD,1.25,,,1201.80,GBP,,,,,,,,,,,,,1.80,GBP,,,,",
            "Limit sell,2024-06-27 15:45:10.456,US0000000200,ACME,Acme Corp,,id-3,"
            "4.0000000000,160.00,USD,1.28,46.29,GBP,498.09,GBP,,,,,,,,0.05,,,,,1.87,GBP,,USD,,",
        )
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec) == {"Opening Balance": Decimal("0"), "Closing Balance": Decimal("6")}


class TestOverlappingExports:
    def test_dividend_without_id_deduplicated_across_layouts(self, tmp_path):
        """The same dividend in a 2024-layout and a 2026-layout export counts once."""
        d = tmp_path / "exports"
        d.mkdir()
        _write(
            d / "a.csv",
            HEADER_2024,
            "Dividend (Dividend),2024-03-23 18:35:26,US0000000002,FOO,\"Foo Company\","
            "24.0000000000,0.03,USD,Not available,,,0.57,\"GBP\",0.13,USD,,,,,,",
        )
        _write(
            d / "b.csv",
            HEADER_2026,
            "Dividend (Dividend),2024-03-23 18:35:26.000,US0000000002,FOO,Foo Company,,,"
            "24.0000000000,0.03,USD,,,,0.57,GBP,0.13,USD,,,,,,,,,,,,,,,,",
        )
        _, divs, _ = T212CsvParser(sorted(str(p) for p in d.glob("*.csv"))).parse()
        assert len(divs) == 1


class TestStockSplits:
    def test_split_pair_across_two_exports(self, tmp_path):
        d = tmp_path / "exports"
        d.mkdir()
        _write(
            d / "1.csv",
            HEADER_2026,
            "Market buy,2024-06-03 10:00:00,US0000000002,FOO,Foo Company,,buy-foo,"
            "100.0000000000,20.00,GBP,,,,2000.00,GBP,,,,,,,,,,,,,,,,,,",
            "Stock split close,2024-06-10 07:00:00,US0000000002,FOO,Foo Company,,split-close,"
            "100.0000000000,20.00,GBP,,0.00,GBP,2000.00,GBP,,,,,,,,,,,,,,,,,,",
        )
        _write(
            d / "2.csv",
            HEADER_2026,
            "Stock split open,2024-06-10 07:00:00,US0000000002,FOO,Foo Company,,split-open,"
            "1000.0000000000,2.00,GBP,,,,2000.00,GBP,,,,,,,,,,,,,,,,,,",
            "Market sell,2024-09-02 11:00:00,US0000000002,FOO,Foo Company,,sell-foo,"
            "400.0000000000,3.00,GBP,,400.00,GBP,1200.00,GBP,,,,,,,,,,,,,,,,,,",
        )
        stmt = _importer().import_from(str(d))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec)["Closing Balance"] == Decimal("600")
        assert not any(
            w.category == CriticalWarningCategory.NEGATIVE_BALANCE for w in stmt.critical_warnings
        )

    def test_old_single_row_stock_split(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2020,
            "Market buy,2023-11-25 14:30:08,US0000000300,OLD,\"Old Co\",2.0000000000,"
            "100.00,USD,1.25000,,160.00,,,,,,GBP,,EOF1",
            "Stock Split,2024-03-01 00:00:00,US0000000300,OLD,\"Old Co\",18.0000000000,"
            ",,,,,,,,,,,,",
        )
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec) == {"Opening Balance": Decimal("2"), "Closing Balance": Decimal("20")}

    def test_known_split_without_rows_is_applied_in_csv_mode(self, tmp_path):
        """NVIDIA's 10-for-1 split of 10 June 2024 has no rows in T212 exports."""
        f = _write(
            tmp_path / "a.csv",
            HEADER_2024,
            "Market buy,2024-03-01 15:00:00,US67066G1040,NVDA,\"NVIDIA\",2.0000000000,"
            "800.00,USD,1.25,,\"GBP\",1280.00,\"GBP\",,,,,EOF1,,,",
            "Market sell,2024-09-01 15:00:00,US67066G1040,NVDA,\"NVIDIA\",5.0000000000,"
            "110.00,USD,1.25,,\"GBP\",440.00,\"GBP\",,,,,EOF2,,,",
        )
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec)["Closing Balance"] == Decimal("15")
        split_mutations = [
            s for s in sec.stock if s.mutation and s.referenceDate == date(2024, 6, 10)
        ]
        assert [s.quantity for s in split_mutations] == [Decimal("18")]
        assert any(
            w.category == CriticalWarningCategory.STOCK_SPLIT_MISMATCH and "NVIDIA" in w.message
            for w in stmt.critical_warnings
        )
        assert not any(
            w.category == CriticalWarningCategory.NEGATIVE_BALANCE for w in stmt.critical_warnings
        )

    def test_known_split_not_applied_when_export_states_it(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2024,
            "Market buy,2024-03-01 15:00:00,US67066G1040,NVDA,\"NVIDIA\",2.0000000000,"
            "800.00,USD,1.25,,\"GBP\",1280.00,\"GBP\",,,,,EOF1,,,",
            "Stock split close,2024-06-10 07:00:00,US67066G1040,NVDA,\"NVIDIA\",2.0000000000,"
            "1200.00,USD,1.25,0.00,\"GBP\",1920.00,\"GBP\",,,,,s1,,,",
            "Stock split open,2024-06-10 07:00:00,US67066G1040,NVDA,\"NVIDIA\",20.0000000000,"
            "120.00,USD,1.25,,,1920.00,\"GBP\",,,,,s2,,,",
        )
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec)["Closing Balance"] == Decimal("20")
        assert not any(
            w.category == CriticalWarningCategory.STOCK_SPLIT_MISMATCH
            for w in stmt.critical_warnings
        )

    def test_known_split_applied_backwards_from_live_position(self, tmp_path):
        """API mode: 20 shares today, no trades since 2024 => 2 shares before the split."""
        client = MagicMock(spec=T212ApiClient)
        client.get_account_summary.return_value = {"id": "1", "currency": "GBP"}
        client.get_instruments.return_value = {"NVDA_US_EQ": "STOCK"}
        client.get_orders.return_value = [
            make_order(
                ticker="NVDA_US_EQ", isin="US67066G1040", quantity="2", filled_at=date(2024, 3, 1)
            )
        ]
        client.get_dividends.return_value = []
        client.get_current_positions.return_value = [{"ticker": "NVDA_US_EQ", "quantity": 20}]
        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=client,
        ):
            stmt = _importer(api_key="k").import_from(str(tmp_path))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec) == {"Opening Balance": Decimal("0"), "Closing Balance": Decimal("20")}


class TestShareMovements:
    def test_transfer_in_and_stock_distribution_applied_with_warning(self, tmp_path):
        f = _write(
            tmp_path / "a.csv",
            HEADER_2026,
            "Transfer in,2024-02-01 10:00:00,US0000000500,XFER,Xfer Inc,,t1,"
            "5.0000000000,,USD,,,,,,,,,,,,,,,,,,,,,,,",
            "Stock distribution,2024-03-01 10:00:00,US0000000500,XFER,Xfer Inc,,t2,"
            "1.0000000000,,USD,,,,,,,,,,,,,,,,,,,,,,,",
            "Transfer out,2024-04-01 10:00:00,US0000000500,XFER,Xfer Inc,,t3,"
            "2.0000000000,,USD,,,,,,,,,,,,,,,,,,,,,,,",
        )
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        assert _balances(sec)["Closing Balance"] == Decimal("4")
        assert any("share movements without cash" in w.message for w in stmt.critical_warnings)
