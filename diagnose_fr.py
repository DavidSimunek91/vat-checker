#!/usr/bin/env python3
"""
Controlled experiment against the FR VIES gateway to get real numbers
behind two open questions:

  1. Does a slower fixed pace measurably reduce FR's error rate, or is the
     flakiness independent of how fast we ask?
  2. Is checkVatApprox (asks France to also match trader name/address -
     a heavier query) more error-prone than plain checkVat (just a yes/no
     validity check)?

Three back-to-back phases, each against 15 FRESH (never-queried) synthetic
FR VAT numbers so no phase benefits from another's cache/warm state:
  A) checkVatApprox @ 2s fixed delay
  B) checkVatApprox @ 5s fixed delay
  C) plain checkVat  @ 2s fixed delay

Records per-call latency and outcome, then prints error rate + latency
percentiles per phase. Standalone - does not touch vat_checker.py, its
cache, or its output files.

Caveat: if vat_checker.py (or anything else) is running concurrently
against the same FR gateway while this runs, results reflect "our pace on
top of whatever else is hitting FR right now", not FR in isolation.
"""

import random
import statistics
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

ENDPOINT = "https://ec.europa.eu/taxation_customs/vies/services/checkVatService"
NS = "urn:ec.europa.eu:taxud:vies:services:checkVat:types"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
REQUESTER_COUNTRY = "CZ"
REQUESTER_VAT = "14890992"


def gen_fr_vat():
    siren = random.randint(100_000_000, 999_999_999)
    key = (12 + 3 * (siren % 97)) % 97
    return f"{key:02d}{siren:09d}"


def build_check_vat_approx(vat_number):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="{soap_ns}" xmlns:urn="{ns}">'
        "<soapenv:Header/><soapenv:Body><urn:checkVatApprox>"
        "<urn:countryCode>FR</urn:countryCode>"
        f"<urn:vatNumber>{vat_number}</urn:vatNumber>"
        f"<urn:requesterCountryCode>{REQUESTER_COUNTRY}</urn:requesterCountryCode>"
        f"<urn:requesterVatNumber>{REQUESTER_VAT}</urn:requesterVatNumber>"
        "</urn:checkVatApprox></soapenv:Body></soapenv:Envelope>"
    ).format(soap_ns=SOAP_NS, ns=NS)


def build_check_vat(vat_number):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="{soap_ns}" xmlns:urn="{ns}">'
        "<soapenv:Header/><soapenv:Body><urn:checkVat>"
        "<urn:countryCode>FR</urn:countryCode>"
        f"<urn:vatNumber>{vat_number}</urn:vatNumber>"
        "</urn:checkVat></soapenv:Body></soapenv:Envelope>"
    ).format(soap_ns=SOAP_NS, ns=NS)


def call_once(body, timeout=25):
    req = urllib.request.Request(
        ENDPOINT, data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        latency = time.monotonic() - t0
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return latency, "ERROR", "IO_ERROR"
        if root.find(".//faultstring") is not None:
            return latency, "ERROR", root.find(".//faultstring").text.strip()
        valid_el = root.find(".//{%s}valid" % NS)
        valid = valid_el.text.strip() if valid_el is not None and valid_el.text else "?"
        return latency, "OK", f"valid={valid}"
    except urllib.error.HTTPError as e:
        latency = time.monotonic() - t0
        raw = e.read()
        try:
            root = ET.fromstring(raw)
            fault = root.find(".//faultstring")
            code = fault.text.strip() if fault is not None and fault.text else "TECHNICAL_ERROR"
        except ET.ParseError:
            code = "TECHNICAL_ERROR"
        return latency, "ERROR", code
    except Exception as e:
        latency = time.monotonic() - t0
        return latency, "ERROR", f"{type(e).__name__}: {e}"


def run_phase(label, build_fn, delay, n):
    print(f"\n=== Phase {label}: n={n}, delay={delay}s ===")
    results = []
    for i in range(n):
        vat = gen_fr_vat()
        latency, status, detail = call_once(build_fn(vat))
        results.append((latency, status, detail))
        print(f"  [{i+1}/{n}] FR{vat} {status:6s} {latency:5.2f}s  {detail}")
        time.sleep(delay)
    return results


def summarize(label, results):
    latencies = [r[0] for r in results]
    errors = [r for r in results if r[1] == "ERROR"]
    error_codes = {}
    for _, _, detail in errors:
        error_codes[detail] = error_codes.get(detail, 0) + 1
    print(f"\n--- {label} summary ---")
    print(f"  n={len(results)}  errors={len(errors)} ({100*len(errors)/len(results):.0f}%)")
    print(f"  latency: min={min(latencies):.2f}s  median={statistics.median(latencies):.2f}s  "
          f"p90={sorted(latencies)[int(0.9*len(latencies))-1]:.2f}s  max={max(latencies):.2f}s")
    if error_codes:
        print(f"  error codes: {error_codes}")


def main():
    random.seed()
    phase_a = run_phase("A: checkVatApprox @ 2s", build_check_vat_approx, 2.0, 15)
    phase_b = run_phase("B: checkVatApprox @ 5s", build_check_vat_approx, 5.0, 15)
    phase_c = run_phase("C: checkVat (plain) @ 2s", build_check_vat, 2.0, 15)

    summarize("A: checkVatApprox @ 2s", phase_a)
    summarize("B: checkVatApprox @ 5s", phase_b)
    summarize("C: checkVat (plain) @ 2s", phase_c)

    print("\nCaveat: if vat_checker.py (or anything else) was running concurrently "
          "against the same FR gateway during this experiment, results show our pace "
          "on top of whatever else was hitting FR, not FR in total isolation.")


if __name__ == "__main__":
    main()
