"""End-to-end run of the Revolut importer through the CLI, on synthetic data."""

from pathlib import Path

import lxml.etree as ET
from typer.testing import CliRunner

from opensteuerauszug.steuerauszug import app

runner = CliRunner()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_TOML = """
[general]
full_name = "Max Muster"
canton = "ZH"
experimental_importers = {experimental}

[brokers.revolut.accounts.main]
kind = "revolut"
account_number = "RV-1"
"""

EXPORT = """Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate
2024-02-01T09:00:00.000Z,,CASH TOP-UP,,,USD 2000,USD,1.0
2024-02-01T14:30:00.123456Z,AAPL,BUY - MARKET,5,USD 150.00,USD 750,USD,1.0
2025-02-14T13:00:00.000Z,AAPL,DIVIDEND,,,USD 1.06,USD,1.0
2025-05-10T15:00:00.000Z,AAPL,SELL - MARKET,2,USD 180.00,USD 360.00,USD,1.0
"""


class _LocalXsdResolver(ET.Resolver):
    def __init__(self, specs_dir: Path) -> None:
        super().__init__()
        self._specs_dir = specs_dir

    def resolve(self, url, pubid, context):
        if not url:
            return None
        candidate = self._specs_dir / url.rsplit("/", 1)[-1]
        if candidate.exists():
            return self.resolve_filename(str(candidate), context)
        return None


def _run(tmp_path: Path, experimental: bool):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TOML.format(experimental=str(experimental).lower()), encoding="utf-8"
    )
    export = tmp_path / "revolut.csv"
    export.write_text(EXPORT, encoding="utf-8")
    output_xml = tmp_path / "revolut.xml"
    result = runner.invoke(
        app,
        [
            "process",
            str(export),
            "--importer",
            "revolut",
            "--tax-year",
            "2025",
            "--config",
            str(config_path),
            "--kursliste-dir",
            str(PROJECT_ROOT / "tests" / "samples" / "kursliste"),
            "--tax-calculation-level",
            "minimal",
            "--output",
            str(tmp_path / "revolut.pdf"),
            "--xml-output",
            str(output_xml),
        ],
    )
    return result, output_xml


def test_revolut_importer_requires_experimental_importers(tmp_path: Path):
    result, _ = _run(tmp_path, experimental=False)

    assert result.exit_code != 0


def test_revolut_export_produces_a_schema_valid_statement(tmp_path: Path):
    result, output_xml = _run(tmp_path, experimental=True)

    assert result.exit_code == 0, f"CLI failed:\n{result.stdout}\n{result.exception!r}"
    assert "Revolut import complete." in result.stdout

    specs_dir = PROJECT_ROOT / "specs"
    xsd_parser = ET.XMLParser()
    xsd_parser.resolvers.add(_LocalXsdResolver(specs_dir))
    schema = ET.XMLSchema(ET.parse(str(specs_dir / "eCH-0196-2-2.xsd"), parser=xsd_parser))
    xml_doc = ET.parse(str(output_xml))
    assert schema.validate(xml_doc), f"XSD validation failed:\n{schema.error_log}"

    ns = "{http://www.ech.ch/xmlns/eCH-0196/2}"
    (security,) = xml_doc.getroot().findall(f".//{ns}security")
    assert security.get("securityName") == "AAPL"
    balances = {
        s.get("referenceDate"): s.get("quantity")
        for s in security.findall(f"{ns}stock")
        if s.get("mutation") == "0"
    }
    # Opening balance carried in from 2024; closing quantity on the tax value.
    assert balances == {"2025-01-01": "5"}
    assert security.find(f"{ns}taxValue").get("quantity") == "3"
    (account,) = xml_doc.getroot().findall(f".//{ns}bankAccount")
    assert account.get("bankAccountNumber") == "RV-1-USD"


PERSONAL_STATEMENT = """Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
Topup,Savings,2025-01-02 10:00:00,2025-01-02 10:00:00,To USD Savings,500.00,0.00,USD,COMPLETED,500.00
Interest,Savings,2025-06-30 23:00:00,2025-07-01 00:30:00,Interest earned,3.25,0.00,USD,COMPLETED,503.25
"""


def test_personal_account_interest_reaches_a_schema_valid_statement(tmp_path: Path):
    folder = tmp_path / "revolut"
    folder.mkdir()
    (folder / "account-statement.csv").write_text(PERSONAL_STATEMENT, encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML.format(experimental="true"), encoding="utf-8")
    output_xml = tmp_path / "revolut.xml"

    result = runner.invoke(
        app,
        [
            "process",
            str(folder),
            "--importer",
            "revolut",
            "--tax-year",
            "2025",
            "--config",
            str(config_path),
            "--kursliste-dir",
            str(PROJECT_ROOT / "tests" / "samples" / "kursliste"),
            "--tax-calculation-level",
            "minimal",
            "--output",
            str(tmp_path / "revolut.pdf"),
            "--xml-output",
            str(output_xml),
        ],
    )

    assert result.exit_code == 0, f"CLI failed:\n{result.stdout}\n{result.exception!r}"
    specs_dir = PROJECT_ROOT / "specs"
    xsd_parser = ET.XMLParser()
    xsd_parser.resolvers.add(_LocalXsdResolver(specs_dir))
    schema = ET.XMLSchema(ET.parse(str(specs_dir / "eCH-0196-2-2.xsd"), parser=xsd_parser))
    xml_doc = ET.parse(str(output_xml))
    assert schema.validate(xml_doc), f"XSD validation failed:\n{schema.error_log}"

    ns = "{http://www.ech.ch/xmlns/eCH-0196/2}"
    (account,) = xml_doc.getroot().findall(f".//{ns}bankAccount")
    assert account.get("bankAccountNumber") == "RV-1-SAVINGS-USD"
    (payment,) = account.findall(f"{ns}payment")
    assert payment.get("amount") == "3.25"
    # Foreign bank interest is revenue without Swiss withholding (column B).
    assert payment.get("grossRevenueB") is not None
