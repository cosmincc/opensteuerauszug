import pytest
from typer.testing import CliRunner
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from opensteuerauszug.steuerauszug import app, expand_amble_paths

runner = CliRunner()


def create_dummy_pdf(path: Path, pages: int = 1):
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(100, 100, f"Page {i+1}")
        c.showPage()
    c.save()


def create_dummy_xml(path: Path):
    path.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<eCH-0196:taxStatement xmlns:eCH-0196="http://www.ech.ch/xmlns/eCH-0196/2" minorVersion="2">
    <eCH-0196:institution>
        <eCH-0196:name>Test Bank</eCH-0196:name>
        <eCH-0196:address>
            <eCH-0196:name>Test Bank</eCH-0196:name>
            <eCH-0196:street>Street</eCH-0196:street>
            <eCH-0196:zip>8000</eCH-0196:zip>
            <eCH-0196:city>Zurich</eCH-0196:city>
        </eCH-0196:address>
    </eCH-0196:institution>
    <eCH-0196:client>
        <eCH-0196:clientNumber>12345</eCH-0196:clientNumber>
        <eCH-0196:person>
            <eCH-0196:firstName>John</eCH-0196:firstName>
            <eCH-0196:lastName>Doe</eCH-0196:lastName>
        </eCH-0196:person>
    </eCH-0196:client>
    <eCH-0196:taxPeriod>2023</eCH-0196:taxPeriod>
    <eCH-0196:periodFrom>2023-01-01</eCH-0196:periodFrom>
    <eCH-0196:periodTo>2023-12-31</eCH-0196:periodTo>
</eCH-0196:taxStatement>
""")


@pytest.fixture
def dummy_xml(tmp_path):
    xml_path = tmp_path / "input.xml"
    create_dummy_xml(xml_path)
    return xml_path


@pytest.fixture
def pre_amble_pdf(tmp_path):
    pdf_path = tmp_path / "pre.pdf"
    create_dummy_pdf(pdf_path, pages=1)
    return pdf_path


@pytest.fixture
def post_amble_pdf(tmp_path):
    pdf_path = tmp_path / "post.pdf"
    create_dummy_pdf(pdf_path, pages=2)
    return pdf_path


def test_cli_concatenation(tmp_path, dummy_xml, pre_amble_pdf, post_amble_pdf):
    output_pdf = tmp_path / "output.pdf"

    # Mock render_tax_statement to produce a dummy PDF
    with patch("opensteuerauszug.steuerauszug.render_tax_statement") as mock_render:

        def side_effect(statement, output_path, **kwargs):
            # output_path should be the temp path
            create_dummy_pdf(Path(output_path), pages=3)
            return Path(output_path)

        mock_render.side_effect = side_effect

        with patch("opensteuerauszug.steuerauszug.TotalCalculator") as MockTotalCalculator:
            MockTotalCalculator.return_value.calculate.side_effect = lambda x: x

            result = runner.invoke(
                app,
                [
                    "process",
                    str(dummy_xml),
                    "--output",
                    str(output_pdf),
                    "--raw-import",
                    "--phases",
                    "render",
                    "--pre-amble",
                    str(pre_amble_pdf),
                    "--post-amble",
                    str(post_amble_pdf),
                    "--tax-year",
                    "2023",
                    "--period-from",
                    "2023-01-01",
                    "--period-to",
                    "2023-12-31",
                    "--tax-calculation-level",
                    "none",
                ],
            )

    print(result.stdout)
    assert result.exit_code == 0
    assert output_pdf.exists()

    reader = PdfReader(output_pdf)
    # 1 page pre + 3 pages main + 2 pages post = 6 pages
    assert len(reader.pages) == 6


def test_cli_concatenation_failure_cleanup(tmp_path, dummy_xml, pre_amble_pdf):
    output_pdf = tmp_path / "output_fail.pdf"

    with (
        patch("opensteuerauszug.steuerauszug.render_tax_statement") as mock_render,
        patch("opensteuerauszug.steuerauszug.PdfWriter") as MockPdfWriter,
    ):

        def side_effect(statement, output_path, **kwargs):
            create_dummy_pdf(Path(output_path), pages=1)
            return Path(output_path)

        mock_render.side_effect = side_effect

        # Simulate failure during merge
        MockPdfWriter.return_value.append.side_effect = Exception("Merge failed")

        with patch("opensteuerauszug.steuerauszug.TotalCalculator") as MockTotalCalculator:
            MockTotalCalculator.return_value.calculate.side_effect = lambda x: x

            result = runner.invoke(
                app,
                [
                    "process",
                    str(dummy_xml),
                    "--output",
                    str(output_pdf),
                    "--raw-import",
                    "--phases",
                    "render",
                    "--pre-amble",
                    str(pre_amble_pdf),
                    "--tax-year",
                    "2023",
                    "--period-from",
                    "2023-01-01",
                    "--period-to",
                    "2023-12-31",
                    "--tax-calculation-level",
                    "none",
                ],
            )

    assert result.exit_code == 1
    assert "Error during PDF concatenation: Merge failed" in result.stdout

    # Check that temp file is cleaned up
    temp_file = output_pdf.with_suffix(".tmp_main.pdf")
    assert not temp_file.exists()


def _run_process_with_ambles(tmp_path, dummy_xml, output_pdf, amble_args):
    with patch("opensteuerauszug.steuerauszug.render_tax_statement") as mock_render:

        def side_effect(statement, output_path, **kwargs):
            create_dummy_pdf(Path(output_path), pages=3)
            return Path(output_path)

        mock_render.side_effect = side_effect

        with patch("opensteuerauszug.steuerauszug.TotalCalculator") as MockTotalCalculator:
            MockTotalCalculator.return_value.calculate.side_effect = lambda x: x

            return runner.invoke(
                app,
                [
                    "process",
                    str(dummy_xml),
                    "--output",
                    str(output_pdf),
                    "--raw-import",
                    "--phases",
                    "render",
                    *amble_args,
                    "--tax-year",
                    "2023",
                    "--period-from",
                    "2023-01-01",
                    "--period-to",
                    "2023-12-31",
                    "--tax-calculation-level",
                    "none",
                ],
            )


def test_expand_amble_paths_keeps_literal_paths(tmp_path):
    missing = tmp_path / "does_not_exist_yet.pdf"
    existing = tmp_path / "existing.pdf"
    create_dummy_pdf(existing)

    # Literal paths are passed through untouched, even if missing; the
    # existence check happens later with its own error message.
    assert expand_amble_paths([existing, missing], "--pre-amble") == [existing, missing]
    assert expand_amble_paths(None, "--pre-amble") == []


def test_expand_amble_paths_sorts_matches_and_keeps_argument_order(tmp_path):
    stmts = tmp_path / "stmts"
    stmts.mkdir()
    for name in ["b.pdf", "c.pdf", "a.pdf", "notes.txt"]:
        (stmts / name).write_bytes(b"")
    (stmts / "sub.pdf").mkdir()  # directories are never matched
    first = tmp_path / "first.pdf"

    result = expand_amble_paths([first, stmts / "*.pdf", stmts / "a.pdf"], "--post-amble")

    assert result == [
        first,
        stmts / "a.pdf",
        stmts / "b.pdf",
        stmts / "c.pdf",
        stmts / "a.pdf",
    ]


def test_expand_amble_paths_recursive_glob(tmp_path):
    (tmp_path / "x" / "y").mkdir(parents=True)
    (tmp_path / "x" / "top.pdf").write_bytes(b"")
    (tmp_path / "x" / "y" / "deep.pdf").write_bytes(b"")

    result = expand_amble_paths([tmp_path / "x" / "**" / "*.pdf"], "--post-amble")

    assert result == [tmp_path / "x" / "top.pdf", tmp_path / "x" / "y" / "deep.pdf"]


def test_cli_concatenation_with_glob(tmp_path, dummy_xml):
    docs = tmp_path / "docs"
    docs.mkdir()
    create_dummy_pdf(docs / "pre_1.pdf", pages=1)
    create_dummy_pdf(docs / "pre_2.pdf", pages=2)
    create_dummy_pdf(docs / "post_1.pdf", pages=4)
    output_pdf = tmp_path / "output.pdf"

    result = _run_process_with_ambles(
        tmp_path,
        dummy_xml,
        output_pdf,
        ["--pre-amble", str(docs / "pre_*.pdf"), "--post-amble", str(docs / "post_*.pdf")],
    )

    print(result.stdout)
    assert result.exit_code == 0
    assert "Prepending 2 document(s)" in result.stdout
    # 1 + 2 pages pre + 3 pages main + 4 pages post = 10 pages
    assert len(PdfReader(output_pdf).pages) == 10


def test_cli_glob_without_matches_fails(tmp_path, dummy_xml):
    output_pdf = tmp_path / "output.pdf"

    result = _run_process_with_ambles(
        tmp_path,
        dummy_xml,
        output_pdf,
        ["--post-amble", str(tmp_path / "nothing_*.pdf")],
    )

    assert result.exit_code == 1
    assert "--post-amble pattern matched no files" in result.stdout
    assert not output_pdf.exists()
