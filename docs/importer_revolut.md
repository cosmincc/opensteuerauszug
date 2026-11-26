# Revolut Importer Guide

## Disclaimer and User Responsibility

**Important**: The Revolut importer is **experimental**. It has been built from the documented export format and tested on synthetic data only. Revolut's export also leaves out information a Swiss tax statement needs (ISINs and the tax withheld from dividends), so some figures are estimates that you must check. The importer is designed to fail safe: an unknown transaction type, or a sale of more shares than the export shows you holding, stops processing with an error instead of producing a wrong statement. Please [report such cases](https://github.com/vroonhof/opensteuerauszug/issues), with the failing row anonymised.

As stated in the [Disclaimer](user_guide.md#disclaimer-and-user-responsibility):

*   **Verify Thoroughly**: Check all positions, income and totals against the Revolut app and your own records before using the output.
*   **Ultimate Responsibility**: **You, the taxpayer, are solely and ultimately responsible for the accuracy and completeness of your tax return.**

---

## Overview

The importer reads the **Revolut Invest account statement** in CSV format. That file is a transaction log of the investment account: trades, dividends, stock splits, custody fees and cash movements. It contains no holdings snapshot, so the importer rebuilds every position by replaying the account history from the start. **The export must therefore cover the whole history of the account ("All time")**, not just the tax year.

Because the experimental flag is required, enable it in the `[general]` section of `config.toml`:

```toml
[general]
experimental_importers = true
```

## Required Input: Account Statement CSV

*   **How to obtain** (Revolut app):
    1. Open **Invest**.
    2. Select **More** (or the **…** menu), then **Documents**.
    3. Choose **Stocks** (brokerage account), then **Account statement**.
    4. Select **Excel / CSV** as the format (not PDF).
    5. Set the period to **All time**.
    6. Generate and save the file, e.g. as `revolut.csv`.

    Revolut moves these menus around from time to time; the [Revolut help page on trading statements](https://help.revolut.com/help/wealth/stocks/getting-started-with-trading/managing-your-trading-account/trading-statements/accessing-my-trading-statements-and-reports/) has the current steps.

*   **Expected layout**: a header row followed by one row per transaction:

    ```csv
    Date,Ticker,Type,Quantity,Price per share,Total Amount,Currency,FX Rate
    2024-01-04T14:39:38.123456Z,AAPL,BUY - MARKET,2.5,USD 180.00,USD 450.00,USD,1.10
    2024-02-15T13:00:00.000Z,AAPL,DIVIDEND,,,USD 2.04,USD,1.10
    ```

    Columns are read by name, so their order does not matter. Keep the export otherwise unchanged. The parser also accepts these variants:

    *   comma, semicolon or tab separated files, with or without a UTF-8 byte order mark;
    *   amounts written as `USD 1,234.50`, `$1,234.50`, `-$30.93`, `12,50 €` or plain numbers (the currency in an amount must agree with the `Currency` column);
    *   numbers with a decimal comma (`0,76672417`), detected once per file;
    *   timestamps such as `2024-01-04T14:39:38.123Z`, `2024-01-04 14:39:38` or `04.01.2024 14:39:38`;
    *   a file without a header row (the standard column order above is then assumed, with a warning; re-export instead if you can).

*   **Several files**: you can pass a directory instead of a file. Every CSV in it that has the Revolut header is read, and rows repeated in overlapping exports are counted once. Other CSV files in the directory are ignored.

Do **not** commit or share real exports; they contain your full trading history.

## Configuration (`config.toml`)

An account section is optional but recommended:

```toml
[brokers.revolut.accounts.main]
account_number = "REVOLUT-1"          # any identifier; used as depot and account number
full_name = "Max Muster"              # optional, overrides [general]
canton = "ZH"                         # optional, overrides [general]

# Revolut exports tickers only. Map them to ISINs so the Kursliste can find the
# security and the issuer country is known.
isin_map = { AAPL = "US0378331005", VUSA = "IE00B3XXRP09" }

# Withholding tax rates used to gross up dividends (see below), by ticker or by
# two-letter issuer country. US = 0.15 is built in.
dividend_withholding_rates = { TSM = 0.21, NL = 0.15 }

include_cash = true                   # report the reconstructed cash balance
bank_country = "LT"                   # custodian country (Revolut Securities Europe UAB)
```

If no account is configured, the depot and account identifier default to `REVOLUT`.

### The shared security identifiers CSV

Revolut exports tickers only. The importer also reads the shared [security identifiers CSV](config.md#security-identifier-enrichment) (`symbol,isin,valor`), the same file the Schwab and Fidelity importers use, so each ticker only needs to be mapped once for all brokers and years:

```csv
symbol,isin,valor
AAPL,US0378331005,908440
```

`isin_map` in the account settings takes precedence over the CSV. Tickers with no ISIN from either source are listed in a warning during import.

## Running the Importer

```console
opensteuerauszug process path/to/revolut.csv \
  --importer revolut \
  --tax-year 2024 \
  -o steuerauszug_2024.pdf
```

`path/to/revolut.csv` can also be a directory holding one or more exports.

## How the Export Is Interpreted

| `Type` in the CSV                               | Treatment |
| ----------------------------------------------- | --------- |
| `BUY - MARKET`, `BUY - LIMIT` (any `BUY …`)     | Purchase. The unit price is `Total Amount / Quantity`, not the rounded `Price per share`. |
| `SELL - MARKET`, `SELL - LIMIT` (any `SELL …`)  | Sale, priced the same way. |
| `DIVIDEND`                                      | Dividend income, booked gross with withholding tax (see below). |
| `DIVIDEND TAX (CORRECTION)`                     | Change to the tax on a dividend (see below). |
| `STOCK SPLIT`                                   | Adds the stated number of shares (negative for a reverse split); no cash moves. |
| `CUSTODY FEE`                                   | Reduces the cash balance. |
| `CASH TOP-UP`, `CASH WITHDRAWAL`                | Cash into / out of the investment account. |
| `TRANSFER FROM REVOLUT … TO REVOLUT …`          | Ignored: moves cash or holdings between Revolut entities, whose history is already in the same export. |
| anything else                                   | Processing stops with "Unknown Revolut transaction type"; please report it. |

### Dates and time zones

Revolut timestamps every row in UTC. The importer converts each timestamp to Swiss local time (CET in winter, CEST in summer) before taking the date. A trade at 23:30 UTC on 31 December is therefore dated 1 January and belongs to the next tax year.

### Holdings and the tax year

Positions are rebuilt from the whole export. Holdings bought in earlier years appear with their opening balance on 1 January even if they had no trades during the tax year. Positions fully closed before the tax year are left out. If a sale would take a holding below zero, the export is incomplete and processing stops with a request to export **All time**.

### Dividends and withholding tax

A Revolut `DIVIDEND` row is the cash you received **after** foreign withholding tax, and the export does not state that tax. A Swiss tax statement must show the gross dividend and the tax withheld (for example for the DA-1 claim on US dividends). The importer therefore:

1.  Looks up a withholding rate, first by ticker, then by the issuer country taken from the ISIN in `isin_map` (or, without an ISIN, guessed from the currency: USD → US). US is set to 15% by default, the treaty rate Revolut applies once you have signed the W-8BEN form it requires for US shares.
2.  Books the gross dividend `received / (1 − rate)` and the difference as non-recoverable withholding tax in a separate payment.
3.  Adds a **critical warning** for every estimated dividend, shown on the statement's instructions page.

If no rate is known (for example a EUR dividend without an ISIN), the dividend is booked as received and a critical warning tells you to configure `dividend_withholding_rates`. In that case also consider running with `--use-broker-withholding off`: otherwise the Kursliste withholding for that dividend is capped to the zero tax the broker data shows.

Check each dividend's tax in the app (**Invest → Portfolio → Transactions → Dividend**, or the dividend details) and adjust the rate per ticker where it differs.

`DIVIDEND TAX (CORRECTION)` rows usually come as a pair of opposite amounts within seconds, which cancel and are ignored. A correction that is not cancelled is matched to the ticker's dividend on the same day, or otherwise to its only dividend between 30 days before and 5 days after. Revolut has been seen to pay a dividend in full and take the tax afterwards this way, so such a dividend row is taken as the **gross** amount and the correction as its withholding tax, without any estimate. A correction that matches no dividend is booked as a standalone withholding entry with a warning.

### Cash

The cash balance per currency is the running sum of top-ups, withdrawals, trades, dividends, tax corrections and custody fees up to 31 December, and each currency becomes its own bank account (`<account_number>-<currency>`). A balance a few cents below zero (Revolut rounding) is reported as zero. A clearly negative balance means cash movements are missing from the export, for example currency exchanges made outside the investment account; it is reported as zero with a critical warning. Set `include_cash = false` if you prefer to report the cash from Revolut's own statement.

### Security details

*   **Name**: the ticker (the export has no company names). With a Kursliste match the official name and valor are filled in later.
*   **Category**: `FUND` if the configured ISIN starts with `IE` or `LU`, otherwise `SHARE`.
*   **Country**: from the ISIN, otherwise guessed from the currency (USD → US, GBP → GB, CHF → CH), otherwise `US`.

## Known Limitations

*   Dividend withholding tax is estimated, not read from the export (see above).
*   No ISINs or company names are exported; tickers that Revolut renamed (e.g. after a company changes its symbol) appear as separate securities.
*   Corporate actions other than stock splits (spin-offs, mergers, delistings) are not in the known `Type` list and stop processing.
*   Cash is reconstructed, not read from a balance; verify it against Revolut's statement.
*   Only the Invest (stocks) account statement is supported. Savings, crypto and commodities exports are out of scope.

## Credits

The parsing rules follow the Revolut parser of [capital-gains-calculator](https://github.com/cgt-calc/capital-gains-calculator) (`cgt_calc/parsers/revolut.py`) and its documentation, in particular the handling of tax corrections and internal transfers.

---
Return to [User Guide](user_guide.md)
