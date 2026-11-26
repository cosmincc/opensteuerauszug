# Trading212 Importer Guide

The Trading212 importer supports three modes. **Hybrid mode is recommended** — it combines a CSV export (withholding tax, FX rates, interest income) with the live API (accurate position anchors, instrument classification) to produce the most complete tax statement.

| Input path | `api_key` configured? | Mode |
|---|---|---|
| File | Yes | **Hybrid** ← recommended |
| File | No | CSV |
| Directory | Yes | API |

---

## Hybrid Mode (recommended)

Hybrid mode combines the best of both sources:

- **CSV export** — preserves withholding tax, FX rates, interest on cash, and share lending income that the API does not expose.
- **Live API positions** — used as anchors for accurate opening/closing balance reconstruction (backward synthesis instead of forward replay from zero).
- **API instrument metadata** — enables correct security categorisation (ETF, BOND, etc.) and `ignore_crypto` filtering.

### Step 1 — Create an API key

1. Open the Trading212 app.
2. Go to **Settings → API (beta)**.
3. Click **Generate key**.
4. Enable the following **read-only** scopes — no write/trading scopes are needed:
   - Account data (read)
   - Portfolio (read)
   - Orders history (read)
   - Dividends history (read)
   - Instruments metadata (read)
5. Copy the generated key (and secret, if you chose key-pair auth).

### Step 2 — Export your transaction history

1. Open the Trading212 app (web or mobile).
2. Navigate to **History**.
3. Click **Export** and select a date range that covers the full tax year (1 Jan – 31 Dec).
4. Download the CSV file.

> **Tip:** Export a range that starts from the beginning of your account (not just the tax year) so that the CSV contains your full transaction history. In hybrid mode the live API position is used as an anchor anyway, so this is less critical than in CSV-only mode — but a full export ensures accurate dividend and interest totals.

### Step 3 — Configure `config.toml`

```toml
[brokers.trading212.accounts.my_t212]
account_number   = "1234567"    # Your Trading212 customer ID (Profile → Personal details)
account_currency = "CHF"        # Account base currency (EUR, GBP, CHF, …)
country          = "GB"         # Fallback issuer country for securities without an ISIN
ignore_crypto    = true         # Skip CRYPTO / CRYPTOCURRENCY instruments

# Hybrid mode: set api_key to activate (leave commented out for CSV-only):
api_key    = "your_api_key_here"
# api_secret = "your_api_secret_here"   # Only for key-pair auth; omit for legacy single-key

# Per-currency cash closing balances at period end (Dec 31).
# Interest on cash and share lending income are read from the CSV automatically.
# This setting controls the closing balance (Vermögenssteuerwert) in the tax statement.
# cash_balances = { CHF = 5000.00, USD = 100.00 }
```

### Step 4 — Run the importer

```console
opensteuerauszug --importer trading212 --period-from 2024-01-01 --period-to 2024-12-31 transactions.csv
```

### What hybrid mode provides

| Field | Source | Available? |
|---|---|---|
| Buy / sell transactions | CSV | Yes |
| Dividend payments | CSV | Yes |
| Gross dividend per share | CSV | Yes (`Price / share` column) |
| Withholding tax | CSV | Yes |
| FX rate (dividends) | CSV | Yes |
| FX rate (trades) | CSV | Yes |
| Interest on cash | CSV | Yes — aggregated per currency |
| Share lending income | CSV | Yes — aggregated per currency |
| Current live position | API | Yes — used as anchor for balance reconstruction |
| Instrument type (STOCK / ETF / CRYPTO …) | API | Yes — enables `ignore_crypto` filtering |

---

## CSV Mode

Use CSV mode when you do not have (or do not want) an API key. All data comes from the downloaded export; no internet access is required after the export.

> **Limitation:** Without an API key there is no live position anchor. Opening balances are reconstructed by replaying your full transaction history forward from zero. This is only accurate if the export covers your entire account history from day one. For any other scenario, use hybrid mode.

### Exporting your transaction history

1. Open the Trading212 app (web or mobile).
2. Navigate to **History**.
3. Click **Export** and select a date range starting from when you opened your account.
4. Download the CSV file.

### Running the importer

```console
opensteuerauszug --importer trading212 --period-from 2025-01-01 --period-to 2025-12-31 transactions.csv
```

(Ensure `api_key` is **not** set in `config.toml`, otherwise the importer will use hybrid mode.)

Trading212 limits each export to one year. To cover your full history, put all yearly exports
in one directory and pass the directory instead of a file. All `*.csv` files in it are merged;
rows repeated in overlapping exports (same T212 `ID`, or identical content) are counted once:

```console
opensteuerauszug --importer trading212 --period-from 2025-01-01 --period-to 2025-12-31 exports/
```

If a dividend is paid on a security whose purchase is not in the exports, the importer raises a
critical warning, because the reported balances for that security would be too low.

### What the CSV provides

| Field | Available? |
|---|---|
| Buy / sell transactions | Yes |
| Dividend payments | Yes |
| Gross dividend per share | Yes (`Price / share` column) |
| Withholding tax | Yes (`Withholding tax` column, in its own currency) |
| FX rate (instrument → account currency) | Parsed, but not used: the statement uses Kursliste / official CHF rates |
| Interest on cash | Yes — aggregated per currency |
| Share lending income | Yes — aggregated per currency |
| Current live position | No — opening balance inferred from transaction history |
| Instrument type (STOCK / ETF / CRYPTO …) | No — all instruments default to STOCK |

---

## API Mode

API mode fetches everything directly from the Trading212 REST API. Use it only when a CSV export is not available, as the API does not expose withholding-tax data or FX rates for dividends.

Pass **any existing directory that contains no `.csv` files** as the input path — the directory itself is not read, it is only the signal to use API mode (a directory with CSV exports selects CSV or hybrid mode):

```console
opensteuerauszug --importer trading212 --period-from 2024-01-01 --period-to 2024-12-31 .
```

See [Step 1 — Create an API key](#step-1--create-an-api-key) above for how to generate your key.

### What the API provides

| Field | Available? |
|---|---|
| Buy / sell transactions | Yes |
| Dividend payments | Yes |
| Gross dividend per share | Yes (`grossAmountPerShare`) |
| Withholding tax | **No** — not exposed by the T212 dividend API endpoint |
| FX rate for dividends | **No** — not returned by the T212 dividend API endpoint |
| FX rate for trades | Yes (via `walletImpact.fxRate`) |
| Interest on cash | **No** — not available via API |
| Share lending income | **No** — not available via API |
| Current live position | Yes — used as anchor for opening-balance reconstruction |
| Instrument type (STOCK / ETF / CRYPTO …) | Yes (via `/instruments` endpoint) |

---

## Configuration reference

### `account_number` — customer ID

Use your **Trading212 customer ID** (the numeric ID visible in the app under Profile → Personal details). This value is used purely as a label in the eCH-0196 output (`DepotNumber` and `ClientNumber`); it is not used for authentication or API queries.

### `account_currency`

The base currency of your Trading212 Invest account (typically `EUR`, `GBP`, or `CHF`). Used when a security's own currency cannot be determined from the transaction data.

### `country` — fallback issuer country

The `country` attribute in the eCH-0196 `<security>` element represents the **issuer country** of each security. The importer derives this automatically from the first two characters of the ISIN:

- `US0378331005` → `US`
- `DE0005140008` → `DE`
- `IE00B3RBWM25` → `IE`

The `country` setting is used as a **fallback** only when a security has no ISIN or has an international/supra-national ISIN prefix (e.g. `XS` for Eurobonds).

### `ignore_crypto`

When `true` (the default), instruments classified as `CRYPTO` or `CRYPTOCURRENCY` by the `/instruments` API endpoint are skipped entirely. In CSV-only mode, without instrument metadata, all instruments default to `STOCK` so crypto will not be filtered. Use hybrid mode if you hold crypto and want it excluded.

### `cash_balances` — per-currency closing balances

Optional dictionary mapping currency codes to the closing cash balance at period end (Dec 31). Used to populate the `BankAccountTaxValue` (Vermögenssteuerwert) in the tax statement.

```toml
cash_balances = { CHF = 5000.00, USD = 100.00 }
```

Interest on cash and share lending income are parsed automatically from the CSV export and do not require manual configuration. The `cash_balances` setting only controls the **closing balance** shown in the bank account section. If omitted, interest/lending income is still reported but no closing balance is shown.

> **Note:** `cash_balances` must be entered manually in both CSV and hybrid mode. The T212 API only provides the current account balance, not the Dec 31 per-currency balances needed for the wealth-tax assessment.

### Authentication: legacy key vs key-pair

- **Legacy (single key):** leave `api_secret` unset. The key is sent as a raw `Authorization` header.
- **Key-pair:** set both `api_key` and `api_secret`. HTTP Basic auth is used (`api_key:api_secret`).

---

## Known Limitations

### Withholding tax and FX rates not available in API-only mode

The Trading212 `/history/dividends` endpoint does not return withholding-tax data or FX rates for dividends. Use hybrid mode or CSV mode if you need these fields (common for US equities with WHT).

### Opening balance in CSV-only mode

In CSV-only mode there is no live position anchor. The importer reconstructs opening balances by replaying the full transaction history forward from zero. This is only accurate if the export covers the complete account history from day one. Hybrid mode resolves this by using the current API position as an anchor.

### No interest or lending income in API-only mode

The T212 API does not expose interest-on-cash or share-lending-income history. These payments are only available from the CSV export. Use hybrid or CSV mode to include them in the tax statement.

### Single account per run

The importer processes one account per run and refuses to start when several Trading212 accounts are configured. Use a separate config file (`--config`) per account.

### Stock splits and corporate actions in API / hybrid mode

Balances are reconstructed backwards from today's live position. The API order history contains no stock splits, spin-offs or transfers. In hybrid mode, split rows from the CSV are merged in, so pass CSV exports that reach up to today. Otherwise the importer raises a critical warning, and you should check every security that had a split after the tax period.

### How dividends are reported

On a CSV dividend row, `Price / share` is the dividend per share **after** withholding tax, and `Total` is the net amount credited in the account currency. Each dividend becomes a gross payment in the instrument currency (`No. of shares × Price / share + Withholding tax`) and a separate withholding-tax payment in the currency T212 reports it in. If the tax is stated in the account currency, the gross is `Total + Withholding tax` in that currency. This is the same convention as the other importers, and the payment reconciliation relies on it. T212 rounds the tax to cents, so on very small dividends the gross can differ slightly from the Kursliste. The `Exchange rate` on dividend rows is ignored: older exports give `Not available`, and newer ones give it in the opposite direction. Prices quoted in pence (`GBX`) are converted to `GBP`.

### Export formats and dates

All three CSV layouts Trading212 has produced are read: the old one with `Total (GBP)` / `Result (GBP)` columns, the one with `Time` and `Total` + `Currency (Total)`, and the current one with `Time (UTC)` and the extra fee and conversion columns. Trading212 states every time in UTC. Dates are converted to Swiss time (Europe/Zurich), so a trade at 23:30 UTC on 31 December belongs to the next Swiss tax year.

When several exports overlap, rows with a T212 `ID` are counted once by ID. Rows without one, such as dividends, are counted once when their action, time (to the second), ISIN, ticker, shares, price, total and tax all match, even if the two exports use different column layouts.

### Stock splits and share movements

Split rows are applied as quantity changes: `Stock split close` removes the old position and `Stock split open` adds the new one. The halves may sit in different exports. The older single `Stock Split` row adds the shares it states.

Trading212 often has no row at all for splits before December 2024 (for example NVIDIA's 10-for-1 split of 10 June 2024). For a list of well-known splits (the list documented by capital-gains-calculator), the importer adds the missing split itself when you held the share across it and the export has no split row near that date. Each such case raises a critical warning naming the split and the shares added. Any other missing split can still corrupt quantities, so compare the closing balances with your Trading212 statement.

`Transfer in`, `Transfer out`, `Stock distribution` and `Custom stock distribution` rows are applied to the holding and raise a critical warning, because their tax treatment (stock dividend, demerger, takeover, transfer) cannot be told from the row. `Spin off` and any other unknown action is skipped with a critical warning. Card rows, deposits, withdrawals, currency conversions, cashback and result adjustments do not affect securities and are ignored.

### Cash balances

Neither the CSV nor the API provides the cash balance at the end of the period. Without `cash_balances` the importer raises a critical warning and reports no cash wealth.

### `ignore_crypto` in CSV-only mode

T212 CSV exports do not include an instrument-type column. In CSV-only mode all instruments default to type `STOCK`, so crypto holdings will not be filtered by `ignore_crypto = true`. Use hybrid mode if you hold crypto and want it excluded.

---

## Troubleshooting

**`ValueError: Trading212 API mode requires an 'api_key'`**
You passed a directory path but did not set `api_key` in `config.toml`. Either add your API key (and use hybrid or API mode) or pass a CSV file instead.

**`ValueError: input_path '...' must be an existing file (CSV export), a directory containing CSV exports, or an existing directory without CSVs (API mode)`**
The path you provided does not exist. For CSV/hybrid mode, check the path to your exported file. For API mode, pass any existing directory (e.g. `.`).

**Opening balance is 0 / incorrect**
In CSV-only mode, ensure your export covers the full history from account opening, not just the current tax year. Alternatively, switch to hybrid mode — it uses the live API position as an anchor and does not rely on a complete history export.

**Instrument types all showing as SHARE**
The `/instruments` endpoint fetch may have failed (check the log for a warning). The importer continues with all instruments defaulting to `STOCK → SHARE`. Check your API key has the Instruments metadata scope enabled.

**Withholding tax is missing on dividends**
This is expected in API-only mode — switch to hybrid or CSV mode to include withholding-tax data.

**Cash balance / interest not appearing in output**
Interest on cash and share lending income are only available from the CSV export (not the API). Ensure you are using hybrid or CSV mode and that your CSV covers the full tax year. For the closing balance (`Vermögenssteuerwert`), add `cash_balances` to your `config.toml`.

---

## Cross-year balance validation

An optional integration test suite verifies that the closing balance of year N
equals the opening balance of year N+1 for every security across all available
annual exports.

### Setup

Place one CSV export per calendar year in the `/private` directory, named after the
year they cover:

```
t212-export-2021.csv
t212-export-2022.csv
t212-export-2023.csv
...
```

Partial-year files are also picked up as long as the name starts with
`t212-export-YYYY` (e.g. `t212-export-2026-partial.csv`). Files are discovered
automatically by glob at collection time — no code changes needed when adding a
new year.

> **Tip:** For accurate forward-synthesis results in CSV mode, export from
> account inception rather than from the start of each tax year. The merged
> dataset is used for all years simultaneously.

### Running

```console
pytest tests/importers/trading212/test_cross_year_validation.py -v
```

The suite is **skipped automatically** if fewer than two export files are found.

To also see file-loading details (how many orders/dividends per file):

```console
pytest tests/importers/trading212/test_cross_year_validation.py -v --log-cli-level=INFO
```

### Interpreting results

| Outcome | Meaning |
|---|---|
| `PASSED` | Closing balance of year N exactly matches opening balance of year N+1 |
| `XFAIL` | One or more securities have a negative balance whose net mutations in the CSV are also negative — the earliest BUY predates the oldest export (e.g. a warrant received via corporate action). The short test summary (`-rx`, enabled by default) shows the affected tickers and their full order history. |
| `FAILED` (synthesis bug) | A negative balance exists despite the CSV containing enough BUYs to cover it — this indicates a real position-reconstruction bug. |

---

Return to [User Guide](user_guide.md)
