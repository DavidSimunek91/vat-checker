# VAT Checker

Bulk-verify EU VAT IDs against the European Commission's [VIES](https://ec.europa.eu/taxation_customs/vies/) service. Built for accounting workflows that need to check hundreds or thousands of company VAT numbers without doing it one at a time in the VIES web UI.

## Features

- **Zero dependencies** — pure Python standard library, nothing to `pip install`
- **Resilient by default** — retries transient VIES errors automatically instead of giving up on the first hiccup (most VIES error codes turn out to be transient, even ones that sound permanent)
- **Fast** — checks multiple countries in parallel, while keeping requests to any single country sequential so it doesn't trip that country's own rate limit
- **Adaptive pacing** — automatically slows down against a country whose gateway is struggling, and speeds back up once it's healthy again
- **Resumable** — results are cached locally, so an interrupted run can pick up where it left off instead of starting over
- **Accounting-friendly output** — one row per company: `valid` / `not_valid` / `error`, plus company name, address, and a timestamp

## Requirements

- Python 3.8 or later
- No third-party packages

## Quick start

```bash
python3 vat_checker.py --input companies.csv --requester-country CZ --requester-vat 12345678
```

`--requester-country` / `--requester-vat` must be **your own company's** VAT ID. VIES requires this as part of its audit trail, and won't return the company name/address without it — only a bare valid/invalid flag.

### Input format

A CSV with a `vat_id` column, e.g.:

```csv
vat_id
CZ14890992
DE143266280
```

If your VAT numbers don't already carry the two-letter country prefix, add a `country_code` column instead:

```csv
vat_id,country_code
14890992,CZ
143266280,DE
```

See [`vat_ids_template.csv`](vat_ids_template.csv) for a working example.

### Output format

Written to `<input>_results.csv` by default:

| Column | Meaning |
|---|---|
| `input_vat_id` | the VAT ID as given in the input |
| `country_code` / `vat_number` | parsed out from the input |
| `status` | `valid`, `not_valid`, or `error` |
| `trader_name` / `trader_address` | returned by VIES when the ID is valid (blank for some countries) |
| `request_date` | the date VIES itself reports for the check |
| `error_detail` | the VIES error code, when `status` is `error` |
| `checked_at` | local timestamp of when this row was checked |

## Options

| Flag | Default | Description |
|---|---|---|
| `--input` | *(required)* | input CSV path |
| `--output` | `<input>_results.csv` | output CSV path |
| `--requester-country` / `--requester-vat` | *(required)* | your own company's VAT ID |
| `--workers` | `6` | how many countries to check concurrently |
| `--cache-file` | `<input>.cache.json` | resume cache location |
| `--limit N` | — | only process the first N rows (useful for a quick test before a full run) |
| `--no-second-pass` | off | skip the end-of-run retry sweep over rows that ended in `error` |

Try a small batch first:

```bash
python3 vat_checker.py --input companies.csv --requester-country CZ --requester-vat 12345678 --limit 20
```

If your machine might sleep during a long run, wrap the command in `caffeinate` (macOS) so it doesn't pause mid-batch:

```bash
caffeinate -i python3 vat_checker.py --input companies.csv --requester-country CZ --requester-vat 12345678
```

**Only run one instance of this script against VIES at a time.** Some of VIES's rate limits (notably France's) are a shared ceiling on their side, not a per-process limit — two instances running at once will push each other's error rate up.

## Design notes

A few choices here aren't obvious from the code alone:

- **Retry policy is a deny-list, not an allow-list.** Early versions treated only a few specific error codes as worth retrying. Live testing showed that was backwards: calling a handful of "permanently failed" IDs again, seconds later, returned clean valid results most of the time — including for the ominous-sounding `TECHNICAL_ERROR`. So the policy flipped: retry everything except a short list of *genuinely* permanent errors (bad format, blocked, invalid requester info).
- **France gets its own pacing, backed by measurement, not a guess.** A controlled experiment (see [`diagnostics/diagnose_fr.py`](diagnostics/diagnose_fr.py)) found that France's gateway responds in 4-6 seconds even when healthy, and that pushing the gap between request starts past ~9 seconds took its concurrency-limit error rate from 13% to 0% — a sharp threshold, not a gentle curve. The default pacing is tuned to clear that threshold.
- **One thread per country, not one thread per request.** A national VIES gateway is a single shared capacity pool; hammering it with concurrent requests just raises everyone's error rate. Different *countries*, however, don't share that limit, so they run concurrently.
- **The per-country delay adapts to a rolling error rate**, not just "did the last request fail" — a live run showed errors arriving in loose bursts (fail, ok, fail, fail) rather than tidy streaks, which a simple streak counter is slow to notice.

## Helper scripts

- [`generate_test_data.py`](generate_test_data.py) — generates a CSV of synthetic-but-correctly-formatted VAT IDs (including a valid French check digit) for load-testing the pipeline without touching real company data.
- [`diagnostics/`](diagnostics) — one-off scripts used to diagnose VIES error behavior and tune the retry/pacing logic above. Not needed to use the tool; kept for reference.

## License

MIT — see [LICENSE](LICENSE).
