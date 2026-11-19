"""
Optional Czech enrichment (stdlib only). Enabled with `vat_checker.py --cz-enrich`.

Two Czech public sources that, unlike VIES, are fast and stable:

  * ARES (MF CR) REST/JSON: company name, registered address and DIC by ICO.
    Lookup is by ICO, so it applies only to 8-digit numbers (legal entities,
    where DIC = "CZ" + ICO). Sole traders have a birth number as DIC and a
    different ICO, so they are skipped here.
  * MF CR "Registr nespolehlivych platcu DPH" (SOAP): whether the VAT payer
    is flagged unreliable, plus the bank accounts it published. Relevant for
    the recipient's liability for VAT (section 109 of the VAT Act) when paying
    to an unpublished account. Up to 100 DIC per request.

Both endpoints were checked live when this module was written. Neither is an
official guarantee of API stability. Failures here never fail the VIES run:
the affected columns are blank and `cz_enrich_error` says why.
"""

import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ARES_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
MF_URL = "https://adisrws.mfcr.cz/adistc/axis2/services/rozhraniCRPDPH.rozhraniCRPDPHSOAP"
MF_NS = "http://adis.mfcr.cz/rozhraniCRPDPH/"
MF_BATCH = 100          # documented maximum DIC per request
ARES_DELAY = 0.25       # polite pacing, ARES publishes no hard limit we rely on
TIMEOUT = 25

CZ_FIELDS = [
    "cz_ares_name", "cz_ares_address", "cz_ares_dic",
    "cz_unreliable_payer", "cz_published_accounts", "cz_enrich_error",
]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def fetch_ares(ico):
    """Return dict with name/address/dic, or None if ARES has no such ICO."""
    req = urllib.request.Request(ARES_URL.format(ico=ico),
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {
        "name": data.get("obchodniJmeno"),
        "address": (data.get("sidlo") or {}).get("textovaAdresa"),
        "dic": data.get("dic"),
    }


def parse_mf_response(raw):
    """Parse the MF CR response into {dic: {"unreliable": str, "accounts": [str]}}.
    Raises RuntimeError on a non-zero service status."""
    root = ET.fromstring(raw)
    out = {}
    for el in root.iter():
        name = _local(el.tag)
        if name == "status" and el.get("statusCode") not in (None, "0"):
            raise RuntimeError(f"MF CR status {el.get('statusCode')}: {el.get('statusText')}")
        if name == "statusPlatceDPH":
            accounts = []
            for acc in el.iter():
                an = _local(acc.tag)
                if an == "standardniUcet":
                    accounts.append(f"{acc.get('cislo')}/{acc.get('kodBanky')}")
                elif an == "nestandardniUcet":
                    accounts.append(acc.get("cislo") or "")
            out[el.get("dic")] = {
                "unreliable": el.get("nespolehlivyPlatce"),   # ANO / NE / NENALEZEN
                "accounts": [a for a in accounts if a],
            }
    return out


def fetch_unreliable_payers(dics):
    """dics: iterable of DIC numbers WITHOUT the CZ prefix."""
    body = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:roz="%s"><soapenv:Header/><soapenv:Body>'
        "<roz:StatusNespolehlivyPlatceRequest>%s</roz:StatusNespolehlivyPlatceRequest>"
        "</soapenv:Body></soapenv:Envelope>"
    ) % (MF_NS, "".join(f"<roz:dic>{escape(d)}</roz:dic>" for d in dics))
    req = urllib.request.Request(
        MF_URL, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return parse_mf_response(resp.read())


def enrich_rows(numbers, log=lambda m: None):
    """numbers: list of CZ national numbers (digits). Returns
    {number: {cz_* columns}}. Never raises."""
    unique = sorted({n for n in numbers if n and n.isdigit()})
    result = {n: {k: "" for k in CZ_FIELDS} for n in unique}

    # MF CR: batched, one request per 100 numbers.
    for i in range(0, len(unique), MF_BATCH):
        chunk = unique[i:i + MF_BATCH]
        try:
            statuses = fetch_unreliable_payers(chunk)
            for n in chunk:
                st = statuses.get(n)
                if st is None:
                    result[n]["cz_unreliable_payer"] = "NENALEZEN"
                else:
                    result[n]["cz_unreliable_payer"] = st["unreliable"] or ""
                    result[n]["cz_published_accounts"] = ";".join(st["accounts"])
        except Exception as e:          # noqa: BLE001 - enrichment must never abort the run
            log(f"MF CR lookup failed for a batch of {len(chunk)}: {e}")
            for n in chunk:
                result[n]["cz_enrich_error"] = f"MF: {e}"

    # ARES: per ICO, legal entities only (8 digits).
    for n in unique:
        if len(n) != 8:
            continue
        try:
            a = fetch_ares(n)
            if a is None:
                result[n]["cz_ares_name"] = "NOT_FOUND"
            else:
                result[n]["cz_ares_name"] = a["name"] or ""
                result[n]["cz_ares_address"] = a["address"] or ""
                result[n]["cz_ares_dic"] = a["dic"] or ""
        except Exception as e:          # noqa: BLE001
            prev = result[n]["cz_enrich_error"]
            result[n]["cz_enrich_error"] = (prev + "; " if prev else "") + f"ARES: {e}"
        time.sleep(ARES_DELAY)
    return result
