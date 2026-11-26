"""Regression tests for tax-relevant fixes in the Trading212 importer:
dividend gross/withholding split, held positions without activity,
multi-file CSV input, currency normalisation and warnings.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from opensteuerauszug.importers.trading212._models import normalize_currency
from opensteuerauszug.importers.trading212.api_client import T212ApiClient
from opensteuerauszug.importers.trading212.csv_parser import T212CsvParser
from opensteuerauszug.importers.trading212.trading212_importer import Trading212Importer

from .conftest import T212_CSV_HEADERS, make_order, make_settings, write_csv

P_FROM = date(2025, 1, 1)
P_TO = date(2025, 12, 31)

BUY_2024 = {
    "Action": "Market buy",
    "Time": "2024-09-26 15:00:00",
    "ISIN": "US8552441094",
    "Ticker": "SBUX",
    "Name": "Starbucks",
    "No. of shares": "2",
    "Price / share": "95.00",
    "Currency (Price / share)": "USD",
    "Exchange rate": "1.18",
    "Total": "161.00",
    "Currency (Total)": "CHF",
}
DIV_2025 = {
    "Action": "Dividend (Dividend)",
    "Time": "2025-05-30 10:00:00",
    "ISIN": "US8552441094",
    "Ticker": "SBUX",
    "Name": "Starbucks",
    "No. of shares": "2",
    "Price / share": "0.61",
    "Currency (Price / share)": "USD",
    "Exchange rate": "1.23",
    "Total": "0.84",
    "Currency (Total)": "CHF",
    "Withholding tax": "0.18",
    "Currency (Withholding tax)": "USD",
}


def _importer(**settings_kwargs) -> Trading212Importer:
    settings_kwargs.setdefault("account_currency", "CHF")
    settings_kwargs.setdefault("cash_balances", {"CHF": Decimal("0")})
    return Trading212Importer(P_FROM, P_TO, [make_settings(**settings_kwargs)])


class TestCsvDividendParsing:
    def test_dividend_fields(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [DIV_2025])
        _, divs, _ = T212CsvParser(str(f)).parse()
        (d,) = divs
        assert d.quantity == Decimal("2")
        # "Price / share" on a dividend row is after withholding tax
        assert d.gross_per_share is None
        assert d.net_per_share == Decimal("0.61")
        assert d.instrument_currency == "USD"
        assert d.amount_account_currency == Decimal("0.84")
        assert d.account_currency == "CHF"
        assert d.withholding_tax == Decimal("0.18")
        assert d.withholding_currency == "USD"

    def test_statement_dividend_gross_usd_and_separate_wht(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [BUY_2024, DIV_2025])
        stmt = _importer().import_from(str(f))
        sec = stmt.listOfSecurities.depot[0].security[0]
        div, wht = sec.payment
        # gross = 2 x 0.61 (net per share) + 0.18 tax
        assert (div.amount, div.amountCurrency, div.quantity) == (
            Decimal("1.40"),
            "USD",
            Decimal("2"),
        )
        assert div.withHoldingTaxClaim is None
        assert (wht.amountCurrency, wht.nonRecoverableTaxAmountOriginal) == ("USD", Decimal("0.18"))
        assert wht.withHoldingTaxClaim is None
        assert sec.currency == "USD"


class TestGbxNormalisation:
    def test_normalize(self):
        assert normalize_currency(Decimal("150"), "GBX") == (Decimal("1.5"), "GBP")
        assert normalize_currency(Decimal("150"), "GBp") == (Decimal("1.5"), "GBP")
        assert normalize_currency(Decimal("1.5"), "GBP") == (Decimal("1.5"), "GBP")
        assert normalize_currency(None, "usd") == (None, "USD")

    def test_order_price_in_pence_converted(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(
            f,
            [dict(BUY_2024, **{"Price / share": "7500", "Currency (Price / share)": "GBX"})],
        )
        orders, _, _ = T212CsvParser(str(f)).parse()
        assert (orders[0].price, orders[0].currency) == (Decimal("75"), "GBP")


class TestMultiFileCsv:
    def test_directory_of_yearly_exports_merged_and_deduplicated(self, tmp_path):
        d = tmp_path / "exports"
        d.mkdir()
        write_csv(d / "t212-2024.csv", [BUY_2024])
        # Overlapping export repeats the buy (same content) plus the 2025 dividend
        write_csv(d / "t212-2025.csv", [BUY_2024, DIV_2025])
        stmt = _importer().import_from(str(d))
        sec = stmt.listOfSecurities.depot[0].security[0]
        balances = {s.name: s.quantity for s in sec.stock if not s.mutation}
        assert balances == {"Opening Balance": Decimal("2"), "Closing Balance": Decimal("2")}
        assert not any("do not cover the purchase" in w.message for w in stmt.critical_warnings)

    def test_rows_with_same_id_deduplicated(self, tmp_path):
        headers = T212_CSV_HEADERS + ["ID"]
        row = dict(BUY_2024, ID="EOF123")
        for name in ("a.csv", "b.csv"):
            import csv as _csv

            with open(tmp_path / name, "w", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
                w.writeheader()
                w.writerow(row)
        orders, _, _ = T212CsvParser([str(tmp_path / "a.csv"), str(tmp_path / "b.csv")]).parse()
        assert len(orders) == 1

    def test_single_year_export_flags_missing_purchase(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [DIV_2025])
        stmt = _importer().import_from(str(f))
        assert any("do not cover the purchase" in w.message for w in stmt.critical_warnings)


class TestUnknownActions:
    def test_unknown_action_becomes_critical_warning(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [BUY_2024, dict(BUY_2024, Action="Spin off")])
        stmt = _importer().import_from(str(f))
        assert any("Spin off" in w.message for w in stmt.critical_warnings)


class TestTickerDisambiguation:
    def test_same_ticker_different_isin_kept_apart(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [BUY_2024, dict(BUY_2024, ISIN="DE0005557508")])
        stmt = _importer().import_from(str(f))
        assert len(stmt.listOfSecurities.depot[0].security) == 2


class TestHeldPositionsWithoutActivity:
    def _api_client(self, positions, orders):
        client = MagicMock(spec=T212ApiClient)
        client.get_account_summary.return_value = {"id": "1", "currency": "CHF"}
        client.get_instruments.return_value = {"VWRL_EQ": "ETF", "OLD_EQ": "STOCK"}
        client.get_orders.return_value = orders
        client.get_dividends.return_value = []
        client.get_current_positions.return_value = positions
        return client

    def test_api_mode_reports_untouched_holding(self, tmp_path):
        positions = [
            {
                "quantity": 5,
                "instrument": {
                    "ticker": "VWRL_EQ",
                    "isin": "IE00B3RBWM25",
                    "name": "Vanguard FTSE All-World",
                    "currency": "GBX",
                },
            }
        ]
        client = self._api_client(positions, [])
        importer = _importer(api_key="k")
        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=client,
        ):
            stmt = importer.import_from(str(tmp_path))
        (sec,) = stmt.listOfSecurities.depot[0].security
        assert sec.symbol == "VWRL_EQ"
        assert sec.isin == "IE00B3RBWM25"
        assert sec.currency == "GBP"
        assert sec.securityCategory == "FUND"
        balances = {s.name: s.quantity for s in sec.stock if not s.mutation}
        assert balances == {"Opening Balance": Decimal("5"), "Closing Balance": Decimal("5")}
        # No CSV in API mode: corporate actions after the period are invisible
        assert any("stock splits" in w.message for w in stmt.critical_warnings)

    def test_security_sold_after_period_included_via_order_history(self, tmp_path):
        sell_later = make_order(
            side="SELL", ticker="OLD_EQ", quantity="3", filled_at=date.today() - timedelta(days=1)
        )
        client = self._api_client([], [sell_later])
        importer = _importer(api_key="k")
        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=client,
        ):
            stmt = importer.import_from(str(tmp_path))
        (sec,) = stmt.listOfSecurities.depot[0].security
        balances = {s.name: s.quantity for s in sec.stock if not s.mutation}
        assert balances["Closing Balance"] == Decimal("3")

    def test_fully_closed_before_period_is_skipped(self, tmp_path):
        old = make_order(side="BUY", ticker="OLD_EQ", filled_at=date(2023, 1, 1))
        old_sell = make_order(side="SELL", ticker="OLD_EQ", filled_at=date(2023, 6, 1))
        client = self._api_client([], [old, old_sell])
        importer = _importer(api_key="k")
        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=client,
        ):
            stmt = importer.import_from(str(tmp_path))
        assert stmt.listOfSecurities is None


class TestHybridIsinBridgeFallback:
    def test_positions_bridge_csv_ticker_when_instruments_fail(self, tmp_path):
        f = tmp_path / "a.csv"
        write_csv(f, [BUY_2024, DIV_2025])
        client = MagicMock(spec=T212ApiClient)
        client.get_instruments_extended.side_effect = RuntimeError("no scope")
        client.get_orders.return_value = []
        client.get_current_positions.return_value = [
            {"quantity": 2, "instrument": {"ticker": "SBUX_US_EQ", "isin": "US8552441094"}}
        ]
        importer = _importer(api_key="k")
        with patch(
            "opensteuerauszug.importers.trading212.trading212_importer.T212ApiClient",
            return_value=client,
        ):
            stmt = importer.import_from(str(f))
        (sec,) = stmt.listOfSecurities.depot[0].security
        assert sec.symbol == "SBUX_US_EQ"
        balances = {s.name: s.quantity for s in sec.stock if not s.mutation}
        assert balances == {"Opening Balance": Decimal("2"), "Closing Balance": Decimal("2")}
