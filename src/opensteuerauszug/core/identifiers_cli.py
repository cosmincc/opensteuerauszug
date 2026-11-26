"""Interactive ticker -> ISIN / valor lookup and the ``identifiers`` commands.

:func:`lookup_and_record` finds ISIN / valor numbers for ticker symbols and,
after confirmation, appends them to the security identifiers CSV that the
calculate phase uses for every importer. It backs both
``opensteuerauszug identifiers lookup --ticker ...`` and the
``--lookup-identifiers`` option of ``process``, which collects the symbols
of all imported securities that have neither ISIN nor valor. See
:mod:`opensteuerauszug.core.ticker_lookup` for how matching works.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import typer

from opensteuerauszug.config.paths import (
    get_app_data_dir,
    resolve_kursliste_dir,
    resolve_security_identifiers_file,
)
from opensteuerauszug.core.identifier_loader import SecurityIdentifierMapLoader
from opensteuerauszug.core.ticker_lookup import (
    EXCHANGE_FOR_CURRENCY,
    KurslisteNameIndex,
    MatchResult,
    OpenFigiClient,
    match_ticker,
)
from opensteuerauszug.model.ech0196 import TaxStatement

app = typer.Typer(help="Manage the security identifiers CSV (symbol -> ISIN / valor).")


@app.callback()
def main():
    """Manage the security identifiers CSV."""


def unmapped_symbols(statement: TaxStatement) -> dict[str, str | None]:
    """Symbols (with currency) of securities that have neither ISIN nor valor.

    The key is what the identifiers CSV is matched on: the security's symbol,
    or its name when it has no symbol.
    """
    wanted: dict[str, str | None] = {}
    if not statement.listOfSecurities:
        return wanted
    for depot in statement.listOfSecurities.depot:
        for security in depot.security:
            if security.isin or security.valorNumber:
                continue
            key = security.symbol or security.securityName
            if key and " " not in key.strip():
                wanted.setdefault(key.strip(), security.currency)
    return wanted


def append_identifiers(csv_path: Path, rows: list[tuple[str, str, str]]) -> None:
    """Append ``(symbol, isin, valor)`` rows, creating the file if needed."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not csv_path.exists() or csv_path.stat().st_size == 0
    if not new_file:
        with open(csv_path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) not in (b"\n", b"\r")
    with open(csv_path, "a", encoding="utf-8", newline="") as fh:
        if not new_file and needs_newline:
            fh.write("\n")
        writer = csv.writer(fh, lineterminator="\n")
        if new_file:
            writer.writerow(["symbol", "isin", "valor"])
        writer.writerows(rows)


def _describe(result: MatchResult) -> None:
    typer.echo(f"\n{result.ticker}: OpenFIGI name {result.figi_name or '-'}")
    for i, c in enumerate(result.candidates, start=1):
        e = c.entry
        typer.echo(
            f"  [{i}] {e.full_name} | ISIN {e.isin or '-'} | valor {e.valor or '-'} "
            f"| {e.currency or '-'} {e.country or '-'} | score {c.score:.2f}"
        )
    if not result.confident:
        typer.echo(f"  Not sure: {result.reason}.")


def lookup_and_record(
    wanted: dict[str, str | None],
    *,
    tax_year: int,
    csv_path: Path,
    kursliste_dir: Path | None = None,
    yes: bool = False,
    api_key: str | None = None,
    client: OpenFigiClient | None = None,
) -> dict[str, tuple[str, str]]:
    """Look up ``ticker -> currency`` pairs and append confirmed matches.

    Tickers already mapped in *csv_path* are skipped. Returns the accepted
    ``ticker -> (isin, valor)`` mappings (strings, possibly empty).
    """
    existing = SecurityIdentifierMapLoader(str(csv_path)).load_map()
    known = {s.upper() for s, ids in existing.items() if ids.get("isin") or ids.get("valor")}
    todo = {t: c for t, c in wanted.items() if t.upper() not in known}
    typer.echo(
        f"{len(wanted)} symbol(s); {len(wanted) - len(todo)} already in {csv_path}; "
        f"{len(todo)} to look up."
    )
    if not todo:
        return {}

    index = KurslisteNameIndex.from_directory(resolve_kursliste_dir(kursliste_dir), tax_year)
    typer.echo(f"Loaded {len(index)} Kursliste names for {tax_year}.")

    client = client or OpenFigiClient(get_app_data_dir() / "openfigi_cache.json", api_key=api_key)
    queries = {t: (t, EXCHANGE_FOR_CURRENCY.get(c or "")) for t, c in todo.items()}
    records = client.lookup(list(queries.values()))

    accepted: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for ticker, cur in sorted(todo.items()):
        result = match_ticker(ticker, records.get(queries[ticker], []), index, currency=cur)
        _describe(result)
        choice = None
        if result.confident and yes:
            choice = 1
        elif result.confident:
            if typer.confirm(f"  Use [1] for {ticker}?", default=True):
                choice = 1
        elif result.candidates and not yes:
            answer = typer.prompt(
                f"  Pick 1-{len(result.candidates)} for {ticker}, or Enter to skip",
                default="",
                show_default=False,
            ).strip()
            if answer.isdigit() and 1 <= int(answer) <= len(result.candidates):
                choice = int(answer)
        if choice is None:
            unresolved.append(ticker)
            continue
        entry = result.candidates[choice - 1].entry
        accepted[ticker] = (entry.isin or "", str(entry.valor or ""))

    if accepted:
        append_identifiers(csv_path, [(t, i, v) for t, (i, v) in accepted.items()])
        typer.echo(f"\nAdded {len(accepted)} identifier(s) to {csv_path}.")
    if unresolved:
        typer.echo(
            "Not resolved: "
            + ", ".join(unresolved)
            + f". Add them to {csv_path} by hand (symbol,isin,valor)."
        )
    return accepted


@app.command()
def lookup(
    tickers: list[str] = typer.Option(..., "--ticker", help="Ticker symbol(s) to look up."),
    tax_year: int = typer.Option(..., "--tax-year", help="Kursliste year to match against."),
    currency: str = typer.Option("USD", "--currency", help="Trading currency of the tickers."),
    kursliste_dir: Path | None = typer.Option(
        None, "--kursliste-dir", help="Directory with the Kursliste (as for 'process')."
    ),
    identifiers_csv_path: Path | None = typer.Option(
        None, "--identifiers-csv-path", help="Security identifiers CSV to update."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Accept confident matches without asking. Uncertain ones are still skipped.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--openfigi-api-key",
        envvar="OPENFIGI_API_KEY",
        help="Optional OpenFIGI API key for higher rate limits.",
    ),
):
    """Find ISIN / valor for tickers and add confirmed ones to the identifiers CSV.

    Names come from OpenFIGI (cached locally) and are matched against the
    Kursliste. Every match is shown for confirmation before it is written.
    To look up every unmapped security of a broker export instead, use
    ``process --lookup-identifiers``.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    wanted = {t.strip().upper(): currency.upper() for t in tickers}
    try:
        lookup_and_record(
            wanted,
            tax_year=tax_year,
            csv_path=resolve_security_identifiers_file(identifiers_csv_path),
            kursliste_dir=kursliste_dir,
            yes=yes,
            api_key=api_key,
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1)
