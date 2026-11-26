"""Tests for ticker -> ISIN lookup via OpenFIGI names and the Kursliste (synthetic data)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensteuerauszug.core import identifiers_cli
from opensteuerauszug.core.identifier_loader import SecurityIdentifierMapLoader
from opensteuerauszug.core.ticker_lookup import (
    FigiRecord,
    KurslisteEntry,
    KurslisteNameIndex,
    OpenFigiClient,
    match_ticker,
    name_similarity,
)
from opensteuerauszug.kursliste.converter import convert_kursliste_xml_to_sqlite

KURSLISTE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<kursliste xmlns="http://xmlns.estv.admin.ch/ictax/2.2.0/kursliste" version="2.2.0.0"
    creationDate="2025-02-13T20:19:00" year="2024">
  <share id="1" valorNumber="908440" isin="US0378331005" securityGroup="SHARE"
      securityType="SHARE.COMMON" institutionId="11" institutionName="Apple Inc."
      country="US" currency="USD" nominalValue="0"/>
  <share id="2" valorNumber="29798540" isin="US02079K3059" securityGroup="SHARE"
      securityType="SHARE.COMMON" securityAppendix="Class A" institutionId="12"
      institutionName="Alphabet Inc." country="US" currency="USD" nominalValue="0"/>
  <share id="3" valorNumber="29798545" isin="US02079K1079" securityGroup="SHARE"
      securityType="SHARE.COMMON" securityAppendix="Class C" institutionId="12"
      institutionName="Alphabet Inc." country="US" currency="USD" nominalValue="0"/>
  <share id="4" valorNumber="1243845" isin="US8740391003" securityGroup="SHARE"
      securityType="SHARE.COMMON" securityAppendix="ADR"
      institutionId="13" institutionName="Taiwan Semiconductor Manufacturing Co Ltd"
      country="TW" currency="USD" nominalValue="0"/>
  <share id="5" valorNumber="1111111" isin="TW0002330008" securityGroup="SHARE"
      securityType="SHARE.COMMON" institutionId="13"
      institutionName="Taiwan Semiconductor Manufacturing Co Ltd"
      country="TW" currency="TWD" nominalValue="0"/>
  <share id="6" valorNumber="2222222" isin="US03784Y2000" securityGroup="SHARE"
      securityType="SHARE.COMMON" institutionId="14"
      institutionName="Apple Hospitality REIT Inc." country="US" currency="USD" nominalValue="0"/>
  <fund id="7" valorNumber="4354003" isin="US9220427424" securityGroup="FUND"
      securityType="FUND.DISTRIBUTION" securityName="Vanguard Total World Stock ETF"
      institutionId="15" institutionName="Vanguard Total World Stock ETF"
      country="US" currency="USD" nominalValue="0"/>
</kursliste>
"""

FIGI = {
    "AAPL": [FigiRecord("APPLE INC", "Common Stock", "US")],
    "GOOGL": [FigiRecord("ALPHABET INC-CL A", "Common Stock", "US")],
    "GOOG": [FigiRecord("ALPHABET INC-CL C", "Common Stock", "US")],
    "TSM": [FigiRecord("TAIWAN SEMICONDUCTOR-SP ADR", "ADR", "US")],
    "VT": [FigiRecord("VANGUARD TOT WORLD STK ETF", "ETP", "US")],
    "ZZZZ": [],
}


@pytest.fixture(scope="module")
def index(tmp_path_factory) -> KurslisteNameIndex:
    path = tmp_path_factory.mktemp("kl") / "kursliste_2024.xml"
    path.write_text(KURSLISTE_XML, encoding="utf-8")
    return KurslisteNameIndex.from_xml(path)


def test_kursliste_names_are_read_from_xml(index):
    assert len(index) == 7
    apple = next(e for e in index.entries if e.isin == "US0378331005")
    assert apple.name == "Apple Inc."
    assert apple.valor == 908440
    assert apple.currency == "USD"


def test_kursliste_names_are_read_from_converted_sqlite(tmp_path, index):
    xml = tmp_path / "kursliste_2024.xml"
    xml.write_text(KURSLISTE_XML, encoding="utf-8")
    convert_kursliste_xml_to_sqlite(xml, tmp_path / "kursliste_2024.sqlite")
    xml.unlink()

    from_db = KurslisteNameIndex.from_directory(tmp_path, 2024)

    assert sorted(e.isin for e in from_db.entries) == sorted(e.isin for e in index.entries)
    alphabet_a = next(e for e in from_db.entries if e.isin == "US02079K3059")
    assert alphabet_a.appendix == "Class A"


def test_missing_kursliste_year_names_the_download_command(tmp_path):
    with pytest.raises(FileNotFoundError, match="kursliste download --year 2024"):
        KurslisteNameIndex.from_directory(tmp_path, 2024)


def test_abbreviated_openfigi_name_matches_full_kursliste_name():
    assert name_similarity("VANGUARD TOT WORLD STK ETF", "Vanguard Total World Stock ETF") >= 0.85
    assert name_similarity("APPLE INC", "Apple Inc.") == 1.0
    assert name_similarity("APPLE INC", "Apple Hospitality REIT Inc.") < 0.85


@pytest.mark.parametrize(
    "ticker, isin",
    [
        ("AAPL", "US0378331005"),
        ("GOOGL", "US02079K3059"),
        ("GOOG", "US02079K1079"),
        ("TSM", "US8740391003"),
        ("VT", "US9220427424"),
    ],
)
def test_ticker_is_matched_confidently_to_the_right_listing(index, ticker, isin):
    result = match_ticker(ticker, FIGI[ticker], index, currency="USD")

    assert result.confident, result.reason
    assert result.best.entry.isin == isin


def test_unknown_ticker_is_not_matched(index):
    result = match_ticker("ZZZZ", FIGI["ZZZZ"], index, currency="USD")

    assert not result.confident
    assert result.candidates == []


def test_equally_good_names_are_not_confident():
    twins = KurslisteNameIndex(
        [
            KurslisteEntry("Example Corp", "US0000000001", 101, "USD", "US", "SHARE"),
            KurslisteEntry("Example Corp.", "US0000000002", 102, "USD", "US", "SHARE"),
        ]
    )

    result = match_ticker("EXM", [FigiRecord("EXAMPLE CORP")], twins, currency="USD")

    assert not result.confident
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# OpenFIGI client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeSession:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def post(self, url, json, headers, timeout):
        self.calls.append(json)
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse(
            [
                (
                    {
                        "data": [
                            {"name": r.name, "securityType": r.security_type, "exchCode": "US"}
                            for r in FIGI[job["idValue"]]
                        ]
                    }
                    if FIGI.get(job["idValue"])
                    else {"warning": "No identifier found."}
                )
                for job in json
            ]
        )


def test_openfigi_answers_are_cached_on_disk(tmp_path):
    cache = tmp_path / "cache.json"
    session = _FakeSession()
    client = OpenFigiClient(cache, session=session, sleep=lambda s: None)

    first = client.lookup([("AAPL", "US"), ("ZZZZ", "US")])
    again = OpenFigiClient(cache, session=session, sleep=lambda s: None).lookup([("AAPL", "US")])

    assert first[("AAPL", "US")][0].name == "APPLE INC"
    assert first[("ZZZZ", "US")] == []
    assert again == {("AAPL", "US"): first[("AAPL", "US")]}
    assert len(session.calls) == 1
    assert session.calls[0][0] == {"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}


def test_expired_cache_entries_are_fetched_again(tmp_path):
    cache = tmp_path / "cache.json"
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    cache.write_text(json.dumps({"US:AAPL": {"fetched": old, "records": []}}))
    session = _FakeSession()

    result = OpenFigiClient(cache, session=session, sleep=lambda s: None).lookup([("AAPL", "US")])

    assert result[("AAPL", "US")][0].name == "APPLE INC"
    assert len(session.calls) == 1


def test_requests_are_batched_by_ten_without_api_key(tmp_path):
    session = _FakeSession()
    pauses = []
    client = OpenFigiClient(None, session=session, sleep=pauses.append)
    FIGI.update({f"T{i}": [] for i in range(12)})

    client.lookup([(f"T{i}", "US") for i in range(12)])

    assert [len(c) for c in session.calls] == [10, 2]
    assert pauses == [2.5]


def test_rate_limited_request_is_retried(tmp_path):
    session = _FakeSession([_FakeResponse(None, status=429)])
    waits = []

    result = OpenFigiClient(None, session=session, sleep=waits.append).lookup([("AAPL", "US")])

    assert result[("AAPL", "US")][0].name == "APPLE INC"
    assert waits == [10.0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(tmp_path: Path, monkeypatch, extra_args, input_text=""):
    kl_dir = tmp_path / "kl"
    kl_dir.mkdir()
    (kl_dir / "kursliste_2024.xml").write_text(KURSLISTE_XML, encoding="utf-8")
    ids = tmp_path / "ids.csv"
    ids.write_text("symbol,isin,valor\nMSFT,US5949181045,951692", encoding="utf-8")
    session = _FakeSession()
    monkeypatch.setattr(
        identifiers_cli,
        "OpenFigiClient",
        lambda cache, api_key=None: OpenFigiClient(None, session=session, sleep=lambda s: None),
    )
    result = CliRunner().invoke(
        identifiers_cli.app,
        [
            "lookup",
            *[a for t in ("AAPL", "GOOGL", "ZZZZ", "MSFT") for a in ("--ticker", t)],
            "--tax-year",
            "2024",
            "--kursliste-dir",
            str(kl_dir),
            "--identifiers-csv-path",
            str(ids),
            *extra_args,
        ],
        input=input_text,
    )
    assert result.exit_code == 0, result.output
    return result, ids, session


def test_lookup_with_yes_adds_confident_matches_and_skips_known_and_unknown(tmp_path, monkeypatch):
    result, ids, session = _run_cli(tmp_path, monkeypatch, ["--yes"])

    loaded = SecurityIdentifierMapLoader(str(ids)).load_map()
    assert loaded == {
        "MSFT": {"isin": "US5949181045", "valor": 951692},
        "AAPL": {"isin": "US0378331005", "valor": 908440},
        "GOOGL": {"isin": "US02079K3059", "valor": 29798540},
    }
    assert "Not resolved: ZZZZ" in result.output
    # MSFT was already known, so it was never sent to OpenFIGI.
    assert all(job["idValue"] != "MSFT" for call in session.calls for job in call)


def test_lookup_writes_only_matches_the_user_confirms(tmp_path, monkeypatch):
    # AAPL: accept; GOOGL: decline. (ZZZZ has no candidates, so no prompt.)
    _, ids, _ = _run_cli(tmp_path, monkeypatch, [], input_text="y\nn\n")

    assert set(SecurityIdentifierMapLoader(str(ids)).load_map()) == {"MSFT", "AAPL"}


def test_new_identifiers_file_is_created_with_header(tmp_path):
    path = tmp_path / "sub" / "ids.csv"

    identifiers_cli.append_identifiers(path, [("AAPL", "US0378331005", "908440")])

    assert path.read_text(encoding="utf-8") == "symbol,isin,valor\nAAPL,US0378331005,908440\n"


def test_only_securities_without_isin_or_valor_are_looked_up():
    from datetime import date

    from opensteuerauszug.model.ech0196 import (
        Depot,
        DepotNumber,
        ISINType,
        ListOfSecurities,
        Security,
        TaxStatement,
        ValorNumber,
    )

    def sec(pid, symbol, isin=None, valor=None, currency="USD"):
        return Security(
            positionId=pid,
            currency=currency,
            quotationType="PIECE",
            securityCategory="SHARE",
            securityName=symbol,
            symbol=symbol,
            isin=ISINType(isin) if isin else None,
            valorNumber=ValorNumber(valor) if valor else None,
            country="US",
        )

    statement = TaxStatement(
        minorVersion=1,
        periodFrom=date(2024, 1, 1),
        periodTo=date(2024, 12, 31),
        taxPeriod=2024,
        listOfSecurities=ListOfSecurities(
            depot=[
                Depot(
                    depotNumber=DepotNumber("D1"),
                    security=[
                        sec(1, "AAPL"),
                        sec(2, "MSFT", isin="US5949181045"),
                        sec(3, "NESN", valor=3886335, currency="CHF"),
                        sec(4, "VUSA", currency="EUR"),
                    ],
                )
            ]
        ),
    )

    assert identifiers_cli.unmapped_symbols(statement) == {"AAPL": "USD", "VUSA": "EUR"}
