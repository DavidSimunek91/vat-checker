#!/usr/bin/env python3
"""One-off diagnostic: call VIES directly for a handful of IDs that came back
TECHNICAL_ERROR during a live run, and print the RAW SOAP response so we can
see the actual fault content instead of just the parsed short code. Does not
touch vat_checker.py, its cache, or its output - fully standalone.

This is what showed most "TECHNICAL_ERROR" results are actually transient -
retrying the exact same ID moments later usually returns a clean, valid
response - which is why vat_checker.py's retry policy is a deny-list rather
than only retrying a hand-picked set of "known safe" codes."""
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://ec.europa.eu/taxation_customs/vies/services/checkVatService"
NS = "urn:ec.europa.eu:taxud:vies:services:checkVat:types"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

FAILED = [
    ("FR", "17756808972"),
    ("SE", "832210082092"),
    ("LV", "33707373019"),
    ("FR", "94585669311"),
]

BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soapenv:Envelope xmlns:soapenv="{soap_ns}" xmlns:urn="{ns}">'
    "<soapenv:Header/><soapenv:Body><urn:checkVatApprox>"
    "<urn:countryCode>{country}</urn:countryCode>"
    "<urn:vatNumber>{vat}</urn:vatNumber>"
    "<urn:requesterCountryCode>CZ</urn:requesterCountryCode>"
    "<urn:requesterVatNumber>14890992</urn:requesterVatNumber>"
    "</urn:checkVatApprox></soapenv:Body></soapenv:Envelope>"
)

for country, vat in FAILED:
    body = BODY.format(soap_ns=SOAP_NS, ns=NS, country=country, vat=vat)
    req = urllib.request.Request(
        ENDPOINT, data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST",
    )
    print(f"\n=== {country}{vat} ===")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("HTTP", resp.status)
            print(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print("HTTP", e.code)
        print(e.read().decode("utf-8", "replace"))
    except Exception as e:
        print("EXC", type(e).__name__, e)
    time.sleep(3)
