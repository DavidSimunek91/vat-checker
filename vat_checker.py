#!/usr/bin/env python3
"""
Bulk EU VAT ID checker against the European Commission's VIES SOAP service
(checkVatApprox), built for accounting workflows that must verify hundreds or
thousands of VAT IDs without clicking through the VIES web UI.

What this tool is: a resilience layer around ONE source (VIES), which itself
fronts 27 national databases. It does not look companies up in registers and
says nothing about what a company does or how healthy it is. See README.md.

Design notes (why, not just what):
  - Retries everything EXCEPT a short list of genuinely permanent VIES errors.
    Live testing showed most codes, even "TECHNICAL_ERROR", are transient.
  - One worker thread per country. Requests to the SAME country are strictly
    sequential (a national gateway is one shared capacity pool); different
    countries run concurrently so one slow country does not block the rest.
  - Per-country delay adapts to the error rate over the last 10 calls, plus an
    escalating circuit breaker after 3 consecutive failures.
  - The delay is applied BEFORE the next live call, never after the last one.
  - After the first pass, rows still in "error" get a second pass at TWICE the
    pace delay.
  - France's pacing is backed by a measurement (diagnostics/diagnose_fr.py).
  - Results are cached incrementally (a crash or Ctrl-C loses at most a few
    seconds of work), cache entries expire, and an entry is only reused for the
    same requester and the same name/address query.
  - Obviously malformed IDs are rejected locally (vat_validation.py) and never
    cost a VIES call.

IMPORTANT: only ONE instance may run against VIES at a time (France's
concurrency limit is a shared ceiling). A lock file enforces this.

No third-party dependencies: stdlib only, Python 3.8+.

Exit codes: 0 all rows resolved, 1 usage/fatal error, 2 some rows ended in
"error", 130 interrupted (partial output written).
"""

import argparse
import csv
import hashlib
import http.client
import json
import os
import random
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

import cz_enrichment
import vat_validation as vv
from vat_validation import EU_COUNTRY_CODES, split_vat_id  # noqa: F401  (re-exported)

ENDPOINT = "https://ec.europa.eu/taxation_customs/vies/services/checkVatService"
NS = "urn:ec.europa.eu:taxud:vies:services:checkVat:types"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# ---------------------------------------------------------------- error catalog
# retryable=False means "asking again will not help". Everything not listed
# here is treated as retryable (deny-list policy, see module docstring).
ERROR_CODES = {
    "INVALID_INPUT": {"retryable": False,
                      "meaning": "VIES rejected the country code or number format."},
    "INVALID_REQUESTER_INFO": {"retryable": False,
                               "meaning": "The requester VAT ID (your own company) is wrong."},
    "VAT_BLOCKED": {"retryable": False,
                    "meaning": "VIES blocked the requester VAT ID."},
    "IP_BLOCKED": {"retryable": False,
                   "meaning": "VIES blocked this IP address."},
    "MISSING_COUNTRY_CODE": {"retryable": False,
                             "meaning": "No country code could be determined."},
    "MS_UNAVAILABLE": {"retryable": True,
                       "meaning": "The member state's national database is down or unreachable."},
    "MS_MAX_CONCURRENT_REQ": {"retryable": True,
                              "meaning": "Concurrency ceiling of one member state reached "
                                         "(shared across all clients; slow down)."},
    "GLOBAL_MAX_CONCURRENT_REQ": {"retryable": True,
                                  "meaning": "Global VIES concurrency ceiling reached."},
    "SERVICE_UNAVAILABLE": {"retryable": True, "meaning": "VIES itself is unavailable."},
    "SERVER_BUSY": {"retryable": True, "meaning": "VIES is overloaded."},
    "TIMEOUT": {"retryable": True,
                "meaning": "Network error or timeout (client-side classification)."},
    "TECHNICAL_ERROR": {"retryable": True,
                        "meaning": "Generic VIES fault; usually transient despite the name."},
    "IO_ERROR": {"retryable": True,
                 "meaning": "Unparseable response (client-side classification)."},
}
NON_RETRYABLE_ERRORS = {c for c, v in ERROR_CODES.items() if not v["retryable"]}

STATUSES = {
    "valid": "VIES confirmed the number is currently registered.",
    "not_valid": "VIES answered and the number is not registered (or not active).",
    "invalid_format": "Rejected locally, impossible for that country. No VIES call was made.",
    "error": "VIES could not give an answer after all retries. Not a verdict on the ID.",
    "not_checked": "Run was interrupted before this row was processed.",
}
EXIT_OK, EXIT_FATAL, EXIT_ERRORS, EXIT_INTERRUPTED = 0, 1, 2, 130

# ----------------------------------------------------------------- pacing config
SLOW_COUNTRIES = {"FR"}       # France's national gateway is the flakiest by far
BASE_DELAY = 1.5
# Measured (diagnostics/diagnose_fr.py): FR responds in 4-6 s and concurrency
# rejections drop from 13% to 0% once the gap between request STARTS is ~9 s.
SLOW_BASE_DELAY = 5.0
MAX_DELAY = 30.0
SECOND_PASS_PACE_MULT = 2.0

ROLLING_WINDOW = 10
ROLLING_ERROR_DELAY_MULT = 3.0   # at 100% window error rate, delay = 4x base

CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_BASE_COOLDOWN = 90.0
CIRCUIT_BREAKER_MAX_COOLDOWN = 900.0
CIRCUIT_BREAKER_RESET_STREAK = 10

MAX_RETRIES_PER_CALL = 5
RETRY_BASE_DELAY = 5.0
RETRY_MAX_DELAY = 90.0
HTTP_TIMEOUT = 25

# Used only by --dry-run time estimates. FR is measured; the rest is an
# assumption (not measured), so estimates are indicative.
ASSUMED_RESPONSE_SECONDS = {"FR": 5.0}
ASSUMED_RESPONSE_DEFAULT = 2.0

# ------------------------------------------------------------------ input config
VAT_COLUMN_CANDIDATES = ["vat_id", "vat", "vatid", "vat_number", "dic", "dič", "tax_id"]
COUNTRY_COLUMN_CANDIDATES = ["country_code", "country", "country_iso", "land"]
# Optional columns: when present, VIES also reports whether name/address MATCH.
QUERY_COLUMNS = {
    "trader_name": ["trader_name", "company_name", "name", "nazev", "název"],
    "trader_company_type": ["trader_company_type", "company_type"],
    "trader_street": ["trader_street", "street", "ulice"],
    "trader_postcode": ["trader_postcode", "postcode", "psc", "psč"],
    "trader_city": ["trader_city", "city", "mesto", "město"],
}

MATCH_CODES = {"1": "valid", "2": "not_valid", "3": "not_processed"}

BASE_OUT_FIELDS = [
    "input_vat_id", "country_code", "vat_number", "status",
    "trader_name", "trader_address", "trader_company_type",
    "request_date", "request_identifier",
    "name_match", "company_type_match", "street_match", "postcode_match", "city_match",
    "error_detail", "checked_at", "from_cache",
]

CACHE_VERSION = 2
CACHE_FLUSH_EVERY = 10          # entries
CACHE_FLUSH_SECONDS = 30.0
DEFAULT_CACHE_MAX_AGE_DAYS = 7


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


# ------------------------------------------------------------------ SOAP layer
def build_request_xml(country_code, vat_number, requester_country, requester_vat, query=None):
    """Element ORDER matters in the VIES schema: optional trader fields sit
    between vatNumber and the requester fields. All values are XML-escaped."""
    query = query or {}
    parts = [
        ("countryCode", country_code), ("vatNumber", vat_number),
        ("traderName", query.get("trader_name")),
        ("traderCompanyType", query.get("trader_company_type")),
        ("traderStreet", query.get("trader_street")),
        ("traderPostcode", query.get("trader_postcode")),
        ("traderCity", query.get("trader_city")),
        ("requesterCountryCode", requester_country),
        ("requesterVatNumber", requester_vat),
    ]
    inner = "".join(f"<urn:{tag}>{escape(str(val))}</urn:{tag}>"
                    for tag, val in parts if val not in (None, ""))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:urn="{NS}">'
        "<soapenv:Header/><soapenv:Body>"
        f"<urn:checkVatApprox>{inner}</urn:checkVatApprox>"
        "</soapenv:Body></soapenv:Envelope>"
    )


def text_or_none(el, tag):
    child = el.find("{%s}%s" % (NS, tag))
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _clean(value):
    # VIES uses "---" for "this member state does not disclose it".
    return None if value in (None, "", "---") else value


def parse_response(raw):
    """Parse a checkVatApprox response body into a result dict, or raise
    VatCheckError with the VIES fault code."""
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

    def match(tag):
        v = text_or_none(resp_el, tag)
        return MATCH_CODES.get(v, v) if v else None

    return {
        "valid": text_or_none(resp_el, "valid") == "true",
        "trader_name": _clean(text_or_none(resp_el, "traderName")),
        "trader_address": _clean(text_or_none(resp_el, "traderAddress")),
        "trader_company_type": _clean(text_or_none(resp_el, "traderCompanyType")),
        "request_date": text_or_none(resp_el, "requestDate"),
        "request_identifier": text_or_none(resp_el, "requestIdentifier"),
        "name_match": match("traderNameMatch"),
        "company_type_match": match("traderCompanyTypeMatch"),
        "street_match": match("traderStreetMatch"),
        "postcode_match": match("traderPostcodeMatch"),
        "city_match": match("traderCityMatch"),
    }


def call_vies_once(country_code, vat_number, requester_country, requester_vat,
                   query=None, timeout=HTTP_TIMEOUT):
    body = build_request_xml(country_code, vat_number, requester_country, requester_vat, query)
    req = urllib.request.Request(
        ENDPOINT, data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            fault = ET.fromstring(raw).find(".//faultstring")
            code = fault.text.strip() if fault is not None and fault.text else "TECHNICAL_ERROR"
        except ET.ParseError:
            code = "TECHNICAL_ERROR"
        raise VatCheckError(code)
    except urllib.error.URLError as e:
        raise VatCheckError("TIMEOUT", str(e.reason))
    except (socket.timeout, ConnectionError, http.client.HTTPException) as e:
        raise VatCheckError("TIMEOUT", str(e))
    return parse_response(raw)


def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _error_result(code):
    return {"status": "error", "error": code}


def call_vies_with_retry(country_code, vat_number, requester_country, requester_vat,
                         query=None, stop=None):
    stop = stop or threading.Event()
    attempt = 0
    while True:
        if stop.is_set():
            return _error_result("INTERRUPTED")
        try:
            r = call_vies_once(country_code, vat_number, requester_country, requester_vat, query)
            r["status"] = "valid" if r.pop("valid") else "not_valid"
            r["error"] = None
            return r
        except VatCheckError as e:
            if e.code in NON_RETRYABLE_ERRORS or attempt >= MAX_RETRIES_PER_CALL:
                return _error_result(e.code)
            sleep_for = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt)) + random.uniform(0, 2)
            stop.wait(sleep_for)
            attempt += 1
        except Exception as e:      # genuinely unforeseen: one row's problem, not the run's
            return _error_result(f"UNEXPECTED: {e}")


# ----------------------------------------------------------------------- cache
def query_signature(requester_country, requester_vat, query):
    """A cached answer is only reusable for the same requester and the same
    name/address query, because both change what VIES returns."""
    q = json.dumps(sorted((query or {}).items()), ensure_ascii=False)
    return hashlib.sha1(f"{requester_country}|{requester_vat}|{q}".encode("utf-8")).hexdigest()[:16]


class Cache:
    """Thread-safe JSON cache, flushed incrementally and atomically."""

    def __init__(self, path, max_age_days=DEFAULT_CACHE_MAX_AGE_DAYS, refresh=False):
        self.path = path
        self.max_age = max_age_days * 86400 if max_age_days else 0
        # --refresh: ignore anything cached before this run started, but still
        # reuse answers fetched during it (duplicate rows cost one call, not two).
        self.min_epoch = time.time() if refresh else 0.0
        self.lock = threading.Lock()
        self.entries = {}
        self._dirty = 0
        self._last_flush = time.monotonic()
        self.discarded_old_format = False
        if path and os.path.exists(path):
            self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            self.entries = data.get("entries", {})
        else:
            # Pre-v2 caches lack requestIdentifier, expiry and requester info.
            self.discarded_old_format = True

    def get(self, key, sig):
        with self.lock:
            e = self.entries.get(key)
        if not e or e.get("status") not in ("valid", "not_valid") or e.get("sig") != sig:
            return None
        if e.get("cached_epoch", 0) < self.min_epoch:
            return None
        if self.max_age and time.time() - e.get("cached_epoch", 0) > self.max_age:
            return None
        return dict(e)

    def put(self, key, sig, result):
        # Only definitive answers are cached; errors are never reused.
        if result.get("status") not in ("valid", "not_valid"):
            return
        entry = dict(result, sig=sig, cached_epoch=time.time())
        with self.lock:
            self.entries[key] = entry
            self._dirty += 1
            due = (self._dirty >= CACHE_FLUSH_EVERY
                   or time.monotonic() - self._last_flush >= CACHE_FLUSH_SECONDS)
        if due:
            self.flush()

    def flush(self):
        if not self.path:
            return
        with self.lock:
            snapshot = json.dumps({"version": CACHE_VERSION, "entries": self.entries},
                                  ensure_ascii=False)
            self._dirty = 0
            self._last_flush = time.monotonic()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(snapshot)
            os.replace(tmp, self.path)


# ------------------------------------------------------------------- run state
class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()

    def tick(self, cache_key, result):
        label = f"error ({result['error']})" if result["status"] == "error" else result["status"]
        if result.get("from_cache"):
            label += " [cache]"
        with self.lock:
            self.done += 1
            print(f"[{self.done}/{self.total}] {cache_key} ... {label}", file=sys.stderr, flush=True)

    def note(self, message):
        with self.lock:
            print(f"    {message}", file=sys.stderr, flush=True)


class RunContext:
    def __init__(self, requester_country, requester_vat, cache, results, stop):
        self.requester_country = requester_country
        self.requester_vat = requester_vat
        self.cache = cache
        self.results = results          # idx -> (row, result); distinct keys per thread
        self.stop = stop


def process_country_group(country_code, items, ctx, progress, pace_mult=1.0):
    """items: list of (idx, row). Requests to this country are sequential.
    The pacing delay is carried in `pending_delay` and paid only before the
    NEXT live call, so there is no pointless wait after the final one."""
    base_delay = (SLOW_BASE_DELAY if country_code in SLOW_COUNTRIES else BASE_DELAY) * pace_mult
    window = deque(maxlen=ROLLING_WINDOW)
    consecutive_errors = 0
    clean_streak = 0
    cooldown_multiplier = 1
    pending_delay = 0.0

    for idx, row in items:
        if ctx.stop.is_set():
            return
        cache_key = f"{country_code}{row['vat_number']}"
        sig = query_signature(ctx.requester_country, ctx.requester_vat, row["query"])

        cached = ctx.cache.get(cache_key, sig)
        if cached:
            cached["from_cache"] = True
            ctx.results[idx] = (row, cached)
            progress.tick(cache_key, cached)
            continue

        if pending_delay and ctx.stop.wait(pending_delay):
            return
        result = call_vies_with_retry(country_code, row["vat_number"], ctx.requester_country,
                                      ctx.requester_vat, row["query"], ctx.stop)
        if result.get("error") == "INTERRUPTED":
            return
        result["checked_at"] = timestamp()
        result["from_cache"] = False
        ctx.cache.put(cache_key, sig, result)
        ctx.results[idx] = (row, result)
        progress.tick(cache_key, result)

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
            pending_delay = min(CIRCUIT_BREAKER_MAX_COOLDOWN,
                                CIRCUIT_BREAKER_BASE_COOLDOWN * cooldown_multiplier)
            progress.note(f"{country_code}: {consecutive_errors} failures in a row, "
                          f"cooling off {pending_delay:.0f}s before the next call")
            cooldown_multiplier = min(8, cooldown_multiplier * 2)
            consecutive_errors = 0
            window.clear()      # pre-outage stats are not informative after it
            continue

        error_rate = sum(window) / len(window) if window else 0.0
        pending_delay = min(MAX_DELAY * pace_mult,
                            base_delay * (1 + ROLLING_ERROR_DELAY_MULT * error_rate))


def order_groups(groups):
    """Largest group first: the longest sequential chain starts earliest, so
    it never ends up queued behind short ones when workers < countries."""
    return sorted(groups.items(), key=lambda kv: -len(kv[1]))


def run_pass(groups, ctx, progress, workers, pace_mult=1.0):
    ordered = order_groups(groups)
    if not ordered:
        return
    workers = max(1, min(workers or len(ordered), len(ordered)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process_country_group, cc, items, ctx, progress, pace_mult)
                   for cc, items in ordered]
        try:
            for fut in futures:
                fut.result()
        except KeyboardInterrupt:
            ctx.stop.set()
            raise


# ------------------------------------------------------------------- lock file
def lock_path():
    return os.path.join(tempfile.gettempdir(), "vat_checker.lock")


def _pid_alive(pid):
    if os.name == "nt":
        return True     # cannot probe safely (os.kill(pid, 0) means CTRL_C_EVENT there)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock():
    """Return an open lock path, or raise RuntimeError if another run is live."""
    path = lock_path()
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return path
        except FileExistsError:
            try:
                with open(path) as f:
                    pid = int((f.read() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                raise RuntimeError(
                    f"another vat_checker run (pid {pid}) holds {path}. VIES limits are shared, "
                    "so run one instance at a time (or pass --no-lock if you are sure).")
            try:
                os.remove(path)     # stale lock from a dead process
            except OSError:
                pass
    raise RuntimeError(f"could not acquire lock {path}")


def release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------- input and output
def read_rows(path, local_validation=True):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        vat_col = find_column(fieldnames, VAT_COLUMN_CANDIDATES)
        country_col = find_column(fieldnames, COUNTRY_COLUMN_CANDIDATES)
        if not vat_col:
            raise ValueError(f"no VAT ID column found. Expected one of {VAT_COLUMN_CANDIDATES}, "
                             f"got columns: {fieldnames}")
        qcols = {k: find_column(fieldnames, cands) for k, cands in QUERY_COLUMNS.items()}
        raw_rows = list(reader)

    rows = []
    for r in raw_rows:
        raw_id = r.get(vat_col, "")
        hint = r.get(country_col) if country_col else None
        cc, number = split_vat_id(raw_id, hint)
        query = {k: (r.get(c) or "").strip() for k, c in qcols.items() if c and (r.get(c) or "").strip()}
        reason = vv.validate_local(cc, number) if local_validation else (
            None if cc else "no country code (no recognised prefix and no country column)")
        rows.append({"raw_id": raw_id, "country_code": cc, "vat_number": number,
                     "query": query, "local_error": reason})
    return rows, {"vat_col": vat_col, "country_col": country_col,
                  "query_cols": {k: c for k, c in qcols.items() if c}}


def result_to_record(row, result, cz=None):
    rec = {f: None for f in BASE_OUT_FIELDS}
    rec.update({
        "input_vat_id": row["raw_id"], "country_code": row["country_code"],
        "vat_number": row["vat_number"], "status": result["status"],
        "error_detail": result.get("error"),
        "checked_at": result.get("checked_at", ""),
        "from_cache": bool(result.get("from_cache")),
    })
    for k in ("trader_name", "trader_address", "trader_company_type", "request_date",
              "request_identifier", "name_match", "company_type_match", "street_match",
              "postcode_match", "city_match"):
        rec[k] = result.get(k)
    if cz is not None:
        rec.update(cz)
    return rec


def write_output(path, fmt, records, fields):
    with open(path, "w", encoding="utf-8", newline="") as f:
        if fmt == "jsonl":
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for rec in records:
                w.writerow({k: ("" if v is None else v) for k, v in rec.items()})


def build_summary(records, duration, exit_code, interrupted):
    per_country = {}
    for rec in records:
        cc = rec["country_code"] or "?"
        c = per_country.setdefault(cc, {
            "rows": 0, "valid": 0, "not_valid": 0, "invalid_format": 0, "error": 0,
            "not_checked": 0, "trader_name_returned": 0, "trader_address_returned": 0,
            "error_codes": Counter()})
        c["rows"] += 1
        c[rec["status"]] += 1
        if rec["status"] == "valid":
            c["trader_name_returned"] += bool(rec["trader_name"])
            c["trader_address_returned"] += bool(rec["trader_address"])
        if rec["status"] == "error":
            c["error_codes"][rec["error_detail"]] += 1
    for c in per_country.values():
        c["error_codes"] = dict(c["error_codes"])
    totals = Counter(rec["status"] for rec in records)
    return {"total_rows": len(records), "status_counts": dict(totals),
            "per_country": per_country, "duration_seconds": round(duration, 1),
            "interrupted": interrupted, "exit_code": exit_code}


# -------------------------------------------------------------- capabilities/info
def build_capabilities():
    """Single source of truth for the machine-readable description. The
    committed capabilities.json is generated from this (and tested for drift)."""
    countries = {}
    for cc in sorted(EU_COUNTRY_CODES):
        countries[cc] = {
            "format": vv.FORMATS[cc],
            "local_checksum": cc in vv.CHECKSUM_COUNTRIES,
            "base_delay_seconds": SLOW_BASE_DELAY if cc in SLOW_COUNTRIES else BASE_DELAY,
        }
    return {
        "tool": "vat-checker",
        "schema_version": 1,
        "purpose": "Bulk-verify EU VAT IDs against VIES with retries, pacing, caching and "
                   "local pre-validation.",
        "not_a": ["company register lookup", "credit or risk check",
                  "source of business activity (NACE) data"],
        "source": {"name": "EU VIES", "protocol": "SOAP checkVatApprox", "endpoint": ENDPOINT,
                   "authority": "VIES is the only authority on whether an ID is registered; "
                                "local checks can only reject impossible IDs."},
        "countries": countries,
        "countries_note": "Greece is 'EL' ('GR' is accepted and mapped). XI = Northern Ireland "
                          "(goods). GB, CH, NO and other non-EU states are not covered by VIES.",
        "input": {
            "format": "CSV, UTF-8 (BOM ok)",
            "vat_column_candidates": VAT_COLUMN_CANDIDATES,
            "country_column_candidates": COUNTRY_COLUMN_CANDIDATES,
            "optional_query_columns": QUERY_COLUMNS,
            "identifier_semantics": "A VAT ID (DIC), not a company registration number (ICO). "
                                    "A bare number is accepted only together with a country "
                                    "column, and only works where the national VAT number equals "
                                    "that bare number (e.g. 8-digit CZ legal entities: DIC = CZ + ICO).",
            "optional_query_note": "When trader name/address columns are present, VIES also "
                                   "returns per-field match flags.",
        },
        "output": {
            "formats": ["csv", "jsonl"],
            "columns": BASE_OUT_FIELDS,
            "cz_columns_with_--cz-enrich": cz_enrichment.CZ_FIELDS,
            "statuses": STATUSES,
            "match_values": sorted(set(MATCH_CODES.values())),
            "null_semantics": "VIES returns '---' when a member state does not disclose a field; "
                              "that is normalised to empty/null.",
            "proof_of_check": "request_identifier is the VIES consultation number; keep it as "
                              "evidence that the check was made on request_date.",
        },
        "errors": ERROR_CODES,
        "retry_policy": {
            "type": "deny-list (retry everything except non-retryable codes)",
            "max_retries_per_call": MAX_RETRIES_PER_CALL,
            "backoff_seconds": {"base": RETRY_BASE_DELAY, "max": RETRY_MAX_DELAY, "jitter": 2},
            "second_pass_pace_multiplier": SECOND_PASS_PACE_MULT,
        },
        "pacing": {
            "model": "one sequential worker per country, countries in parallel",
            "base_delay_seconds": BASE_DELAY,
            "slow_countries": sorted(SLOW_COUNTRIES),
            "slow_base_delay_seconds": SLOW_BASE_DELAY,
            "adaptive": f"delay scales with error rate over last {ROLLING_WINDOW} calls "
                        f"(up to {1 + ROLLING_ERROR_DELAY_MULT:.0f}x), capped at {MAX_DELAY}s",
            "circuit_breaker": {"consecutive_failures": CIRCUIT_BREAKER_THRESHOLD,
                                "base_cooldown_seconds": CIRCUIT_BREAKER_BASE_COOLDOWN,
                                "max_cooldown_seconds": CIRCUIT_BREAKER_MAX_COOLDOWN},
            "single_instance": "enforced by a lock file; VIES limits are shared across clients",
        },
        "cache": {"default_max_age_days": DEFAULT_CACHE_MAX_AGE_DAYS,
                  "stores": ["valid", "not_valid"],
                  "reuse_requires": "same requester and same name/address query",
                  "flush": f"every {CACHE_FLUSH_EVERY} new entries or {CACHE_FLUSH_SECONDS:.0f}s, atomic"},
        "exit_codes": {str(EXIT_OK): "all rows resolved (valid/not_valid/invalid_format)",
                       str(EXIT_FATAL): "usage or fatal error",
                       str(EXIT_ERRORS): "finished, but some rows ended in 'error'",
                       str(EXIT_INTERRUPTED): "interrupted, partial output written"},
        "optional_modules": {"cz_enrichment": "ARES (name/address/DIC) and MF CR unreliable-payer "
                                              "register with published bank accounts, via --cz-enrich"},
    }


def estimate_seconds(groups, workers):
    """Rough wall time: per-country sequential chains scheduled on `workers`
    threads (longest first). Response times other than FR are assumed."""
    chains = []
    for cc, items in groups.items():
        resp = ASSUMED_RESPONSE_SECONDS.get(cc, ASSUMED_RESPONSE_DEFAULT)
        delay = SLOW_BASE_DELAY if cc in SLOW_COUNTRIES else BASE_DELAY
        chains.append((cc, len(items) * (resp + delay) - delay))
    chains.sort(key=lambda x: -x[1])
    slots = [0.0] * max(1, min(workers or len(chains), len(chains) or 1))
    for _, t in chains:
        i = slots.index(min(slots))
        slots[i] += t
    return chains, max(slots) if slots else 0.0


def fmt_duration(s):
    s = int(round(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


# ------------------------------------------------------------------------ main
def build_parser():
    p = argparse.ArgumentParser(description="Bulk-check EU VAT IDs via VIES.")
    p.add_argument("--input", help="input CSV")
    p.add_argument("--output", help="default: <input>_results.csv (or .jsonl)")
    p.add_argument("--format", choices=["csv", "jsonl"], default="csv", help="output format")
    p.add_argument("--requester-country",
                   help="your own company's country code, e.g. CZ (VIES needs it for its audit "
                        "trail and to return company name/address)")
    p.add_argument("--requester-vat", help="your own VAT number (country prefix is stripped)")
    p.add_argument("--cache-file", default=None, help="default: <input>.cache.json")
    p.add_argument("--cache-max-age-days", type=float, default=DEFAULT_CACHE_MAX_AGE_DAYS,
                   help=f"reuse cached answers up to this age (default {DEFAULT_CACHE_MAX_AGE_DAYS}, "
                        "0 = never expire)")
    p.add_argument("--refresh", action="store_true", help="ignore cached answers (still writes them)")
    p.add_argument("--workers", type=int, default=0,
                   help="concurrent country groups (default 0 = one per country present)")
    p.add_argument("--limit", type=int, default=None, help="only process the first N rows")
    p.add_argument("--no-second-pass", action="store_true",
                   help="skip the slower end-of-run retry sweep over rows that errored")
    p.add_argument("--no-local-validation", action="store_true",
                   help="send every ID to VIES, even ones that fail local format/checksum checks")
    p.add_argument("--no-lock", action="store_true", help="do not enforce a single running instance")
    p.add_argument("--cz-enrich", action="store_true",
                   help="add ARES and MF CR unreliable-payer data for CZ rows (cz_* columns)")
    p.add_argument("--summary-json", help="write a per-country machine-readable run summary here")
    p.add_argument("--dry-run", action="store_true",
                   help="parse and validate the input, print counts and a time estimate, no network")
    p.add_argument("--capabilities", action="store_true",
                   help="print the machine-readable capability description (JSON) and exit")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.capabilities:
        print(json.dumps(build_capabilities(), ensure_ascii=False, indent=2))
        return EXIT_OK
    if not args.input:
        parser.error("--input is required")
    if not args.dry_run and not (args.requester_country and args.requester_vat):
        parser.error("--requester-country and --requester-vat are required (unless --dry-run)")

    ext = "jsonl" if args.format == "jsonl" else "csv"
    output_path = args.output or f"{os.path.splitext(args.input)[0]}_results.{ext}"
    try:
        rows, info = read_rows(args.input, local_validation=not args.no_local_validation)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_FATAL
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)

    groups, local_failed = {}, 0
    for idx, row in enumerate(rows):
        if row["local_error"]:
            local_failed += 1
            continue
        groups.setdefault(row["country_code"], []).append((idx, row))

    print(f"Loaded {total} rows from {args.input} (vat column: '{info['vat_col']}', "
          f"country column: '{info['country_col'] or '-'}', "
          f"name/address columns: {sorted(info['query_cols']) or '-'})", file=sys.stderr)
    print(f"Rejected locally (no VIES call): {local_failed}", file=sys.stderr)
    print("Countries: " + ", ".join(f"{cc}={len(v)}" for cc, v in order_groups(groups)),
          file=sys.stderr)

    if args.dry_run:
        chains, wall = estimate_seconds(groups, args.workers)
        print("\nEstimated sequential time per country (upper bound, no cache; only FR response "
              "time is measured, others assumed 2 s):", file=sys.stderr)
        for cc, t in chains:
            print(f"  {cc}: {fmt_duration(t)}", file=sys.stderr)
        print(f"Estimated wall time: {fmt_duration(wall)}", file=sys.stderr)
        for row in rows:
            if row["local_error"]:
                print(f"  invalid_format: {row['raw_id']!r}: {row['local_error']}", file=sys.stderr)
        return EXIT_OK

    requester_country = vv.canonical_country(args.requester_country)
    requester_vat = vv.normalise(args.requester_vat)
    if requester_vat.startswith(requester_country):
        requester_vat = requester_vat[len(requester_country):]

    lock = None
    if not args.no_lock:
        try:
            lock = acquire_lock()
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_FATAL

    started = time.monotonic()
    cache_path = args.cache_file or (args.input + ".cache.json")
    cache = Cache(cache_path, args.cache_max_age_days, refresh=args.refresh)
    if cache.discarded_old_format:
        print("Note: ignoring an old-format cache (no consultation numbers or expiry).",
              file=sys.stderr)
    stop = threading.Event()
    results = {}
    ctx = RunContext(requester_country, requester_vat, cache, results, stop)
    interrupted = False

    try:
        for idx, row in enumerate(rows):       # local rejections need no network
            if row["local_error"]:
                results[idx] = (row, {"status": "invalid_format", "error": row["local_error"],
                                      "checked_at": timestamp()})

        try:
            progress = Progress(sum(len(v) for v in groups.values()))
            run_pass(groups, ctx, progress, args.workers)

            if not args.no_second_pass and not stop.is_set():
                failed = [(i, r) for i, (r, res) in results.items() if res["status"] == "error"]
                if failed:
                    print(f"\nFirst pass done. Retrying {len(failed)} failed row(s) at "
                          f"{SECOND_PASS_PACE_MULT:.0f}x the delay...\n", file=sys.stderr)
                    retry_groups = {}
                    for i, r in failed:
                        retry_groups.setdefault(r["country_code"], []).append((i, r))
                    run_pass(retry_groups, ctx, Progress(len(failed)),
                             min(args.workers or 3, 3),
                             pace_mult=SECOND_PASS_PACE_MULT)
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            print("\nInterrupted: saving cache and partial output...", file=sys.stderr)
    finally:
        cache.flush()
        if lock:
            release_lock(lock)

    cz_data = {}
    if args.cz_enrich and not interrupted:
        cz_numbers = [r["vat_number"] for r, res in results.values()
                      if r["country_code"] == "CZ" and res["status"] in ("valid", "not_valid")]
        print(f"CZ enrichment for {len(set(cz_numbers))} number(s)...", file=sys.stderr)
        cz_data = cz_enrichment.enrich_rows(cz_numbers, log=lambda m: print(f"    {m}", file=sys.stderr))

    records = []
    for idx, row in enumerate(rows):
        row_, result = results.get(idx, (row, {"status": "not_checked"}))
        cz = None
        if args.cz_enrich:
            cz = cz_data.get(row["vat_number"]) if row["country_code"] == "CZ" else None
            cz = cz or {k: "" for k in cz_enrichment.CZ_FIELDS}
        records.append(result_to_record(row_, result, cz))
    fields = BASE_OUT_FIELDS + (cz_enrichment.CZ_FIELDS if args.cz_enrich else [])
    write_output(output_path, args.format, records, fields)

    counts = Counter(rec["status"] for rec in records)
    if interrupted:
        code = EXIT_INTERRUPTED
    elif counts.get("error"):
        code = EXIT_ERRORS
    else:
        code = EXIT_OK
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as f:
            json.dump(build_summary(records, time.monotonic() - started, code, interrupted),
                      f, ensure_ascii=False, indent=2)

    print(f"\nDone. Results written to {output_path}")
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return code


if __name__ == "__main__":
    sys.exit(main())
