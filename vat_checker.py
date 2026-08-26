#!/usr/bin/env python3
"""
Bulk EU VAT ID checker against the European Commission's VIES SOAP service
(checkVatApprox) - built for accounting workflows that need to verify
hundreds or thousands of company VAT IDs without doing it one-by-one in
the VIES web UI.

Design notes (why it's built this way, not just what it does):
  - Retries everything EXCEPT a short list of genuinely permanent VIES
    errors (bad format, blocked, invalid requester). Live testing showed
    most other error codes - including ones that sound permanent, like
    "TECHNICAL_ERROR" - are actually transient: a bare retry moments later
    often returns a clean, valid response.
  - One worker thread per country. Requests to the SAME country stay
    strictly sequential (a national VIES gateway is one shared capacity
    pool - hammering it concurrently just raises everyone's error rate),
    but different countries run concurrently, so one slow/flaky country
    doesn't block the rest of the batch.
  - Per-country delay is adaptive: it's driven by the error rate over the
    last 10 calls to that country (not just a fixed number), plus an
    escalating circuit breaker if a country fails 3 times in a row.
  - After the first full pass, any row still in "error" gets one more
    pass at a slower, more conservative pace.
  - A controlled experiment against France's gateway (see
    diagnostics/diagnose_fr.py) measured the real threshold instead of
    guessing: France's own response time is already 4-6s, and pushing the
    gap between request starts past ~9s took its concurrency-limit error
    rate from 13% to 0%. The default pacing for France is tuned to that
    measurement.

IMPORTANT: only run ONE instance of this script against VIES at a time.
MS_MAX_CONCURRENT_REQ (France's concurrency limit) is a real shared
ceiling on VIES's side, not a per-process limit - running two instances
in parallel pushes both of their error rates up.

No third-party dependencies - stdlib only, Python 3.8+.

Usage:
    python3 vat_checker.py --input companies.csv

See README.md for the full CSV format and command-line options.
"""

import argparse
import csv
import http.client
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor

ENDPOINT = "https://ec.europa.eu/taxation_customs/vies/services/checkVatService"
NS = "urn:ec.europa.eu:taxud:vies:services:checkVat:types"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Deny-list, not allow-list: retry anything except errors that are
# genuinely permanent regardless of how many times you ask again.
NON_RETRYABLE_ERRORS = {
    "INVALID_INPUT",
    "INVALID_REQUESTER_INFO",
    "VAT_BLOCKED",
    "IP_BLOCKED",
    "MISSING_COUNTRY_CODE",
}

SLOW_COUNTRIES = {"FR"}       # France's national gateway is the flakiest by far
BASE_DELAY = 1.5

# A controlled experiment (3 phases x 15 live FR checkVatApprox calls)
# measured this directly instead of guessing: FR's own response time is
# already 4-6s, and MS_MAX_CONCURRENT_REQ (always this exact code, always
# a fast ~0.2-0.4s rejection - a real concurrency ceiling, not a generic
# flake) showed up at a 13% rate when the gap between request STARTS
# (response time + our delay) was ~8s, and at 0% when that gap was ~9s.
# That's a sharp threshold, not a gentle curve, so the fixed floor needs
# to clear it on its own - 5.0s (+ the ~5s response time) lands past the
# 9s mark that measured clean.
SLOW_BASE_DELAY = 5.0
MAX_DELAY = 30.0

# A live 1000-row run showed FR errors arriving in loose bursts (e.g. rows
# 433, 436, 439, 440 - failed/ok/failed/failed/failed), not just tidy
# back-to-back streaks. A simple "N in a row" counter catches the second
# pattern but is slow to notice the first. So the per-request delay is
# driven by the error RATE over the last ROLLING_WINDOW calls to this
# country, not just the current streak - it reacts as soon as things start
# going wrong more often, even with a stray success mixed in.
ROLLING_WINDOW = 10
ROLLING_ERROR_DELAY_MULT = 3.0  # at 100% error rate in the window, delay = 4x base

# Belt-and-suspenders on top of the rolling delay: 3 STRICTLY consecutive
# failures still means "this country's gateway is actively down right now",
# worth a real pause rather than just a longer per-request delay. Repeated
# trips escalate the cooldown (it means the earlier, shorter pause wasn't
# enough) and it resets after a solid run of successes.
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_BASE_COOLDOWN = 90.0
CIRCUIT_BREAKER_MAX_COOLDOWN = 900.0
CIRCUIT_BREAKER_RESET_STREAK = 10

MAX_RETRIES_PER_CALL = 5
RETRY_BASE_DELAY = 5.0
RETRY_MAX_DELAY = 90.0

VAT_COLUMN_CANDIDATES = ["vat_id", "vat", "vatid", "vat_number", "dic", "tax_id"]
COUNTRY_COLUMN_CANDIDATES = ["country_code", "country", "country_iso", "land"]

EU_COUNTRY_CODES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",
}


class VatCheckError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def find_column(fieldnames, candidates):
    lowered = {f.lower().strip(): f for f in fieldnames}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None


def split_vat_id(raw_id, country_hint=None):
    raw_id = (raw_id or "").strip().upper().replace(" ", "")
    m = re.match(r"^([A-Z]{2})([A-Z0-9]+)$", raw_id)
    if m and m.group(1) in EU_COUNTRY_CODES:
        return m.group(1), m.group(2)
    if country_hint:
        country_hint = country_hint.strip().upper()
        if country_hint == "GR":
            country_hint = "EL"
        return country_hint, raw_id
    return None, raw_id


def build_request_xml(country_code, vat_number, requester_country, requester_vat):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="{soap_ns}" xmlns:urn="{ns}">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<urn:checkVatApprox>"
        "<urn:countryCode>{country}</urn:countryCode>"
        "<urn:vatNumber>{vat}</urn:vatNumber>"
        "<urn:requesterCountryCode>{rcountry}</urn:requesterCountryCode>"
        "<urn:requesterVatNumber>{rvat}</urn:requesterVatNumber>"
        "</urn:checkVatApprox>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    ).format(
        soap_ns=SOAP_NS, ns=NS, country=country_code, vat=vat_number,
        rcountry=requester_country, rvat=requester_vat,
    )


def text_or_none(el, tag):
    child = el.find("{%s}%s" % (NS, tag))
    if child is None or child.text is None:
        return None
    return child.text.strip()


def call_vies_once(country_code, vat_number, requester_country, requester_vat, timeout=25):
    body = build_request_xml(country_code, vat_number, requester_country, requester_vat)
    req = urllib.request.Request(
        ENDPOINT,
        data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            root = ET.fromstring(raw)
            fault = root.find(".//faultstring")
            code = fault.text.strip() if fault is not None and fault.text else "TECHNICAL_ERROR"
        except ET.ParseError:
            code = "TECHNICAL_ERROR"
        raise VatCheckError(code)
    except urllib.error.URLError as e:
        raise VatCheckError("TIMEOUT", str(e.reason))
    except (socket.timeout, ConnectionError, http.client.HTTPException) as e:
        raise VatCheckError("TIMEOUT", str(e))

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise VatCheckError("IO_ERROR", str(e))

    resp_el = root.find(".//{%s}checkVatApproxResponse" % NS)
    if resp_el is None:
        fault = root.find(".//faultstring")
        if fault is not None and fault.text:
            raise VatCheckError(fault.text.strip())
        raise VatCheckError("TECHNICAL_ERROR", "unexpected response shape")

    valid_text = text_or_none(resp_el, "valid")
    return {
        "valid": valid_text == "true",
        "trader_name": text_or_none(resp_el, "traderName"),
        "trader_address": text_or_none(resp_el, "traderAddress"),
        "request_date": text_or_none(resp_el, "requestDate"),
    }


def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def call_vies_with_retry(country_code, vat_number, requester_country, requester_vat):
    attempt = 0
    while True:
        try:
            r = call_vies_once(country_code, vat_number, requester_country, requester_vat)
            return {
                "status": "valid" if r["valid"] else "not_valid",
                "trader_name": r["trader_name"],
                "trader_address": r["trader_address"],
                "request_date": r["request_date"],
                "error": None,
            }
        except VatCheckError as e:
            if e.code in NON_RETRYABLE_ERRORS or attempt >= MAX_RETRIES_PER_CALL:
                return {
                    "status": "error",
                    "trader_name": None,
                    "trader_address": None,
                    "request_date": None,
                    "error": e.code,
                }
            sleep_for = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt)) + random.uniform(0, 2)
            time.sleep(sleep_for)
            attempt += 1
        except Exception as e:
            # Anything genuinely unforeseen: one row's problem, not the
            # whole run's.
            return {
                "status": "error",
                "trader_name": None,
                "trader_address": None,
                "request_date": None,
                "error": f"UNEXPECTED: {e}",
            }


def load_cache(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache_locked(path, cache, lock):
    if not path:
        return
    with lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
        os.replace(tmp, path)


class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()

    def tick(self, cache_key, result):
        if result["status"] == "error":
            label = f"error ({result['error']})"
        else:
            label = result["status"]
        with self.lock:
            self.done += 1
            print(f"[{self.done}/{self.total}] {cache_key} ... {label}", flush=True)

    def note(self, message):
        with self.lock:
            print(f"    {message}", flush=True)


def process_country_group(country_code, items, requester_country, requester_vat,
                           cache, cache_lock, progress):
    """items: list of (index, raw_id, vat_number). Requests to this one
    country are made strictly sequentially - a national VIES gateway is a
    single shared capacity pool, so parallel hits to the SAME country just
    trade a slower row for a higher error rate on all of them."""
    base_delay = SLOW_BASE_DELAY if country_code in SLOW_COUNTRIES else BASE_DELAY
    window = deque(maxlen=ROLLING_WINDOW)
    consecutive_errors = 0
    clean_streak = 0
    cooldown_multiplier = 1
    out = {}

    for idx, raw_id, vat_number in items:
        cache_key = f"{country_code}{vat_number}"

        with cache_lock:
            cached = cache.get(cache_key)
        if cached and cached.get("status") in ("valid", "not_valid"):
            result = dict(cached)
            progress.tick(cache_key, result)
            out[idx] = (raw_id, country_code, vat_number, result)
            continue

        result = call_vies_with_retry(country_code, vat_number, requester_country, requester_vat)
        result["checked_at"] = timestamp()
        with cache_lock:
            cache[cache_key] = result
        progress.tick(cache_key, result)
        out[idx] = (raw_id, country_code, vat_number, result)

        is_error = result["status"] == "error"
        window.append(1 if is_error else 0)
        if is_error:
            consecutive_errors += 1
            clean_streak = 0
        else:
            consecutive_errors = 0
            clean_streak += 1
            if clean_streak >= CIRCUIT_BREAKER_RESET_STREAK:
                cooldown_multiplier = 1

        if consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            cooldown = min(CIRCUIT_BREAKER_MAX_COOLDOWN,
                            CIRCUIT_BREAKER_BASE_COOLDOWN * cooldown_multiplier)
            progress.note(f"{country_code}: {consecutive_errors} failures in a row, "
                          f"cooling off {cooldown:.0f}s before continuing")
            time.sleep(cooldown)
            cooldown_multiplier = min(8, cooldown_multiplier * 2)
            consecutive_errors = 0
            window.clear()  # stats from before the outage aren't informative after it
            continue  # skip the normal per-request delay below, we just waited plenty

        error_rate = sum(window) / len(window) if window else 0.0
        delay = min(MAX_DELAY, base_delay * (1 + ROLLING_ERROR_DELAY_MULT * error_rate))
        time.sleep(delay)

    return out


def run_pass(groups, requester_country, requester_vat, cache, cache_lock, progress, workers):
    all_results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(process_country_group, cc, items, requester_country, requester_vat,
                        cache, cache_lock, progress)
            for cc, items in groups.items()
        ]
        for fut in futures:
            all_results.update(fut.result())
    return all_results


def main():
    parser = argparse.ArgumentParser(description="Bulk-check EU VAT IDs via VIES.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", help="default: <input>_results.csv")
    parser.add_argument("--requester-country", required=True,
                         help="your own company's country code, e.g. CZ (VIES requires this "
                              "for its audit trail, and needs it to return company name/address)")
    parser.add_argument("--requester-vat", required=True,
                         help="your own company's VAT number, without the country prefix")
    parser.add_argument("--cache-file", default=None, help="default: <input>.cache.json")
    parser.add_argument("--workers", type=int, default=6,
                         help="concurrent country-groups (default: 6)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-second-pass", action="store_true",
                         help="skip the end-of-run retry sweep over rows that errored")
    args = parser.parse_args()

    output_path = args.output or (os.path.splitext(args.input)[0] + "_results.csv")
    cache_path = args.cache_file or (args.input + ".cache.json")
    cache = load_cache(cache_path)
    cache_lock = threading.Lock()

    with open(args.input, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        vat_col = find_column(fieldnames, VAT_COLUMN_CANDIDATES)
        country_col = find_column(fieldnames, COUNTRY_COLUMN_CANDIDATES)
        if not vat_col:
            print(f"ERROR: no VAT ID column found. Expected one of {VAT_COLUMN_CANDIDATES}, "
                  f"got columns: {fieldnames}", file=sys.stderr)
            sys.exit(1)
        rows = list(reader)

    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)

    groups = {}
    for idx, row in enumerate(rows):
        raw_id = row.get(vat_col, "")
        country_hint = row.get(country_col) if country_col else None
        country_code, vat_number = split_vat_id(raw_id, country_hint)
        key = country_code or "?"
        groups.setdefault(key, []).append((idx, raw_id, vat_number))

    print(f"Loaded {total} rows from {args.input} "
          f"(vat column: '{vat_col}', country column: '{country_col or '-'}')")
    print("Countries: " + ", ".join(f"{cc}={len(v)}" for cc, v in
                                     sorted(groups.items(), key=lambda x: -len(x[1]))))
    print(f"Running with {args.workers} concurrent country-groups...\n")

    progress = Progress(total)
    results = run_pass(groups, args.requester_country, args.requester_vat,
                        cache, cache_lock, progress, args.workers)
    save_cache_locked(cache_path, cache, cache_lock)

    if not args.no_second_pass:
        failed_idx = {idx for idx, (_, _, _, r) in results.items() if r["status"] == "error"}
        if failed_idx:
            print(f"\nFirst pass done. Retrying {len(failed_idx)} failed row(s) once more, "
                  "at a slower pace...\n")
            retry_groups = {}
            for idx in failed_idx:
                raw_id, country_code, vat_number, _ = results[idx]
                retry_groups.setdefault(country_code, []).append((idx, raw_id, vat_number))
                # force a fresh attempt, not the cached error
                with cache_lock:
                    cache.pop(f"{country_code}{vat_number}", None)

            retry_progress = Progress(len(failed_idx))
            retry_workers = max(1, min(args.workers, 3))
            retry_results = run_pass(retry_groups, args.requester_country, args.requester_vat,
                                      cache, cache_lock, retry_progress, retry_workers)
            results.update(retry_results)
            save_cache_locked(cache_path, cache, cache_lock)

    out_fields = [
        "input_vat_id", "country_code", "vat_number", "status",
        "trader_name", "trader_address", "request_date", "error_detail", "checked_at",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=out_fields)
        writer.writeheader()
        for idx in range(total):
            raw_id, country_code, vat_number, result = results[idx]
            writer.writerow({
                "input_vat_id": raw_id,
                "country_code": country_code,
                "vat_number": vat_number,
                "status": result["status"],
                "trader_name": result.get("trader_name"),
                "trader_address": result.get("trader_address"),
                "request_date": result.get("request_date"),
                "error_detail": result.get("error"),
                "checked_at": result.get("checked_at", ""),
            })

    counts = {}
    for _, (_, _, _, r) in results.items():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nDone. Results written to {output_path}")
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
