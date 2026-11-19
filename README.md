# VAT Checker

Bulk-verify EU VAT IDs against the European Commission's [VIES](https://ec.europa.eu/taxation_customs/vies/) service, built for accounting workflows that must check hundreds or thousands of VAT numbers without clicking through the VIES web UI.

## What this tool is, and is not

| It is | It is not |
|---|---|
| A resilience layer around **one** source, VIES. VIES itself fronts 27 national databases, and its gateway is flaky (France above all). This tool makes bulk use of it reliable. | A company-register lookup. It does not use ARES, Handelsregister, Infogreffe etc. |
| An answer to "is this VAT ID registered right now, and as whom?" plus a **VIES consultation number** as proof the check was made. | A source of business activity, financial health, insolvency or ownership data. `valid` is not "trustworthy". |
| Offline-first on garbage: IDs that are impossible for their country are rejected locally and never cost a VIES call. | A replacement for VIES: VIES stays the only authority on whether an ID is registered. |

Optional `--cz-enrich` adds two **fast, stable Czech sources** next to VIES (ARES for name/address, the MF ČR unreliable-VAT-payer register with published bank accounts). Those are the "nice API" sources; VIES is the awkward one.

## Input is a VAT ID, not an IČO

The input is the **VAT ID (DIČ)**, normally with its country prefix (`CZ14890992`). A bare number is accepted with a `country_code` column, but then it must already be the national VAT number:

| Case | Works with a bare number? |
|---|---|
| CZ legal entity (8 digits) | Yes, DIČ = `CZ` + IČO |
| CZ sole trader (9-10 digits) | Only if you have the DIČ (a birth number), **not** the IČO |
| FR | No, SIREN is not the VAT number (the VAT number adds a 2-character key) |
| DE | No, the Steuernummer is not the USt-IdNr |

## Countries

27 EU member states plus `XI` (Northern Ireland, goods). Greece is `EL` (`GR` is accepted and mapped). **Not covered:** GB, CH, NO and other non-EU states. VIES does not cover them.

Formats and checksums per country: [`capabilities.json`](capabilities.json). Local checksum validation exists for **CZ** (8-digit), **FR**, **BE**, **DK**, **PL**; all other countries get a format check only. A local rejection only means "impossible for that country"; anything that passes is still sent to VIES.

## What it finds

| Output column | Meaning |
|---|---|
| `status` | `valid`, `not_valid`, `invalid_format` (rejected locally), `error` (VIES gave no answer, not a verdict), `not_checked` (run interrupted) |
| `trader_name`, `trader_address`, `trader_company_type` | as returned by VIES. `---` (member state does not disclose) becomes empty. Which countries disclose what is **not asserted here**: measure it on your data with `--summary-json` (`trader_name_returned` per country). |
| `request_date`, **`request_identifier`** | the VIES consultation number and date: keep it as evidence of the check (reverse charge, intra-EU supplies) |
| `name_match`, `company_type_match`, `street_match`, `postcode_match`, `city_match` | `valid` / `not_valid` / `not_processed`, filled when the input has name/address columns (below) |
| `error_detail`, `checked_at`, `from_cache` | diagnostics |

### Confirming that an ID belongs to the right company

Add any of these optional columns to the input and VIES also reports match flags: `trader_name`, `trader_company_type`, `trader_street`, `trader_postcode`, `trader_city`. That checks that the VAT ID belongs to the company in your ERP, not merely that it exists. **Match flags depend on the member state:** in a live test with a name and city for a CZ company, VIES returned a valid result but no match flags. Verify per country on your own data before relying on them.

## Quick start

```bash
python3 vat_checker.py --input companies.csv --requester-country CZ --requester-vat CZ12345678
```

`--requester-*` is **your own company's** VAT ID. VIES needs it for its audit trail, and without it returns only a bare valid/invalid flag. The country prefix on `--requester-vat` is stripped if present.

Check an input first, without any network call:

```bash
python3 vat_checker.py --input companies.csv --dry-run
```

It lists IDs rejected locally (with the reason), rows per country and an estimated duration. Only France's response time is measured; other countries are assumed at 2 s, so treat estimates as indicative.

### Input format

```csv
vat_id,country_code
CZ14890992,
DE143266280,
45274649,CZ
```

See [`vat_ids_template.csv`](vat_ids_template.csv). Accepted column names are listed in [`capabilities.json`](capabilities.json). Spaces, dots and dashes inside IDs are ignored.

### Options

| Flag | Default | Description |
|---|---|---|
| `--input` | required | input CSV |
| `--output` | `<input>_results.csv` | output path |
| `--format` | `csv` | `csv` or `jsonl` |
| `--requester-country` / `--requester-vat` | required | your own VAT ID (not needed for `--dry-run`) |
| `--workers` | `0` (one per country present) | concurrent country groups |
| `--cache-file` | `<input>.cache.json` | cache location |
| `--cache-max-age-days` | `7` | reuse cached answers up to this age, `0` = never expire |
| `--refresh` | off | ignore anything cached before this run |
| `--limit N` | | only the first N rows |
| `--no-second-pass` | off | skip the slower retry sweep over `error` rows |
| `--no-local-validation` | off | send every ID to VIES, even ones failing local checks |
| `--no-lock` | off | do not enforce a single running instance |
| `--cz-enrich` | off | add ARES + MF ČR columns for CZ rows |
| `--summary-json PATH` | | per-country machine-readable run summary |
| `--dry-run` | off | validate and estimate, no network |
| `--capabilities` | | print the machine-readable capability description and exit |

Exit codes: `0` all rows resolved, `1` usage or fatal error, `2` finished but some rows are `error`, `130` interrupted (partial output written).

If your machine might sleep during a long run, wrap it in `caffeinate` (macOS).

## How it handles VIES problems

| Problem | Handling |
|---|---|
| Transient errors (`MS_UNAVAILABLE`, `TECHNICAL_ERROR`, timeouts, ...) | Retry up to 5 times, exponential backoff 5-90 s plus jitter. Policy is a **deny-list**: retry everything except `INVALID_INPUT`, `INVALID_REQUESTER_INFO`, `VAT_BLOCKED`, `IP_BLOCKED`, `MISSING_COUNTRY_CODE`. |
| Per-country concurrency ceiling (`MS_MAX_CONCURRENT_REQ`) | One **sequential** worker per country, countries in parallel. Delay adapts to the error rate over the last 10 calls (up to 4x). |
| France | 5 s base delay, backed by a measurement ([`diagnostics/diagnose_fr.py`](diagnostics/diagnose_fr.py)): concurrency errors fell from 13% to 0% once the gap between request starts passed ~9 s. |
| A country's gateway goes down | Circuit breaker: 3 consecutive failures trigger a 90 s cooldown, doubling on repeats up to 900 s. |
| Rows still failing after pass 1 | Second pass at **2x the delay**, at most 3 workers. |
| Crash, Ctrl-C, closed laptop | Cache flushes every 10 new answers or 30 s (atomic). Ctrl-C stops cleanly, saves the cache and writes partial output (`not_checked` rows). A rerun resumes from the cache. |
| Two runs hitting VIES at once | A lock file refuses a second instance. VIES limits are a shared ceiling, so two runs raise each other's error rates. |
| Impossible IDs | Rejected locally as `invalid_format`, no call made. |

Error codes with meanings and retryability: [`capabilities.json`](capabilities.json) (`errors`).

### Efficiency notes

- Wall time is set by the **largest country group**, since a country is sequential. France at about 10 s per row means roughly 2.8 h per 1000 FR rows. Nothing in this tool can parallelise within a country without raising the error rate.
- The pacing delay is paid only before the next live call, never after the last one, and never for cache hits.
- Groups are scheduled largest first, so the longest chain starts immediately.
- Duplicates in the input cost one call.
- Cached answers are reused only for the same requester **and** the same name/address query, and expire after `--cache-max-age-days`. Errors are never cached.

## Caching and evidence

For accounting evidence, keep the `request_identifier` and `request_date`. Cached rows carry the original consultation number and `from_cache=true`: a cached answer is **not** a new consultation. If you need a fresh proof for a specific document, run with `--refresh`.

## Czech enrichment (`--cz-enrich`)

Adds columns `cz_ares_name`, `cz_ares_address`, `cz_ares_dic`, `cz_unreliable_payer` (`ANO`/`NE`/`NENALEZEN`), `cz_published_accounts`, `cz_enrich_error` for `CZ` rows.

- **ARES** (REST/JSON) is looked up by IČO, so only for 8-digit numbers (legal entities).
- **MF ČR register of unreliable VAT payers** (SOAP, 100 numbers per request) covers all CZ numbers. Published bank accounts matter when paying: paying to an unpublished account can create VAT liability for the recipient (§ 109 of the Czech VAT Act). This is an accounting hint, not legal advice.
- Both endpoints were verified live when written, neither is an official stability guarantee. A failure fills `cz_enrich_error` and never fails the VIES run.

## Machine-readable description

`python3 vat_checker.py --capabilities` prints the full description as JSON (countries and formats, statuses, error taxonomy, retry and pacing parameters, cache rules, exit codes, output columns). [`capabilities.json`](capabilities.json) is that output, committed. A test fails if the two drift apart, so regenerate with:

```bash
python3 vat_checker.py --capabilities > capabilities.json
```

For an agent or pipeline: use `--format jsonl`, read the exit code, and consume `--summary-json`. Progress goes to stderr; stdout carries only the final summary lines.

## Design notes

- **Retry policy is a deny-list.** Live testing showed that calling "permanently failed" IDs again seconds later mostly returned clean results, even for the ominous `TECHNICAL_ERROR`.
- **One thread per country.** A national gateway is a single shared capacity pool; different countries do not share it.
- **Rolling error rate, not a streak counter.** Errors arrive in loose bursts (fail, ok, fail, fail), which a streak counter notices late.
- **Local validation is conservative.** It never claims an ID is registered, and rejects only what is provably impossible. Checksums exist only where the algorithm is simple and unambiguous.
- **SOAP, not REST.** VIES also exposes a JSON REST endpoint, but it sits behind the same national gateways and returned the same concurrency error in a quick probe, so it would not remove the underlying limits. Migrating is possible later; it is not done here.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline only (VIES and the Czech endpoints are mocked).

## Helper scripts

- [`generate_test_data.py`](generate_test_data.py): synthetic, correctly formatted VAT IDs (including a valid French check digit) for load tests. Not real companies. Most rows will be `not_valid`.
- [`diagnostics/`](diagnostics): one-off scripts used to measure VIES behaviour and tune the retry and pacing logic. Not needed to use the tool.

## Requirements

Python 3.8+, standard library only.

## License

MIT, see [LICENSE](LICENSE).
