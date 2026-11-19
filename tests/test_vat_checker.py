"""Offline unit tests (no network). Run: python3 -m unittest discover -s tests -v"""
import csv
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cz_enrichment  # noqa: E402
import vat_checker as vc  # noqa: E402
import vat_validation as vv  # noqa: E402

OK_XML = b"""<env:Envelope xmlns:env="http://schemas.xmlsoap.org/soap/envelope/"><env:Body>
<ns2:checkVatApproxResponse xmlns:ns2="urn:ec.europa.eu:taxud:vies:services:checkVat:types">
<ns2:countryCode>CZ</ns2:countryCode><ns2:vatNumber>14890992</ns2:vatNumber>
<ns2:requestDate>2026-11-19+01:00</ns2:requestDate><ns2:valid>true</ns2:valid>
<ns2:traderName>IBM Ceska</ns2:traderName><ns2:traderCompanyType>---</ns2:traderCompanyType>
<ns2:traderAddress>V parku 2294/4</ns2:traderAddress>
<ns2:traderNameMatch>1</ns2:traderNameMatch><ns2:traderCityMatch>2</ns2:traderCityMatch>
<ns2:requestIdentifier>WAPIAAAAaHzPoBKL</ns2:requestIdentifier>
</ns2:checkVatApproxResponse></env:Body></env:Envelope>"""


class Validation(unittest.TestCase):
    def test_known_good_ids_pass(self):
        for cc, n in [("CZ", "14890992"), ("CZ", "45274649"), ("BE", "0403019261"),
                      ("DK", "13585628"), ("PL", "7740001454"), ("FR", "40303265045"),
                      ("DE", "143266280"), ("NL", "123456789B01")]:
            self.assertIsNone(vv.validate_local(cc, n), (cc, n))

    def test_checksum_failures(self):
        for cc, n in [("CZ", "12345678"), ("BE", "0403019262"), ("DK", "13585629"),
                      ("PL", "7740001455"), ("FR", "41303265045")]:
            self.assertIsNotNone(vv.validate_local(cc, n), (cc, n))

    def test_format_failures(self):
        self.assertIn("format", vv.validate_local("DE", "12345"))
        self.assertIn("not covered", vv.validate_local("GB", "123456789"))
        self.assertIn("no country", vv.validate_local(None, "123"))

    def test_cz_individual_has_format_check_only(self):
        self.assertIsNone(vv.validate_local("CZ", "7001011234"))

    def test_split_prefix_hint_and_greece(self):
        self.assertEqual(vv.split_vat_id("cz 148.909.92"), (None, "CZ14890992")[:0] or ("CZ", "14890992"))
        self.assertEqual(vv.split_vat_id("14890992", "CZ"), ("CZ", "14890992"))
        self.assertEqual(vv.split_vat_id("GR123456789"), ("EL", "123456789"))
        self.assertEqual(vv.split_vat_id("123456789", "gr"), ("EL", "123456789"))
        self.assertEqual(vv.split_vat_id("123", None), (None, "123"))


class Soap(unittest.TestCase):
    def test_request_escapes_and_orders(self):
        xml = vc.build_request_xml("CZ", "1&2<3", "CZ", "999",
                                   {"trader_name": "A & B <x>", "trader_city": "Praha"})
        self.assertIn("1&amp;2&lt;3", xml)
        self.assertIn("A &amp; B &lt;x&gt;", xml)
        order = [xml.index(t) for t in ("vatNumber", "traderName", "traderCity", "requesterCountryCode")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("traderStreet", xml)

    def test_parse_response(self):
        r = vc.parse_response(OK_XML)
        self.assertTrue(r["valid"])
        self.assertEqual(r["request_identifier"], "WAPIAAAAaHzPoBKL")
        self.assertIsNone(r["trader_company_type"])          # "---" normalised
        self.assertEqual(r["name_match"], "valid")
        self.assertEqual(r["city_match"], "not_valid")
        self.assertIsNone(r["street_match"])

    def test_parse_fault(self):
        fault = b"<e><Body><Fault><faultstring>MS_UNAVAILABLE</faultstring></Fault></Body></e>"
        with self.assertRaises(vc.VatCheckError) as cm:
            vc.parse_response(fault)
        self.assertEqual(cm.exception.code, "MS_UNAVAILABLE")
        with self.assertRaises(vc.VatCheckError) as cm:
            vc.parse_response(b"not xml")
        self.assertEqual(cm.exception.code, "IO_ERROR")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "c.json")

    def test_roundtrip_expiry_signature_and_no_errors(self):
        c = vc.Cache(self.path, max_age_days=1)
        c.put("CZ1", "sigA", {"status": "valid", "trader_name": "X"})
        c.put("CZ2", "sigA", {"status": "error", "error": "TIMEOUT"})
        c.flush()
        c2 = vc.Cache(self.path, max_age_days=1)
        self.assertEqual(c2.get("CZ1", "sigA")["trader_name"], "X")
        self.assertIsNone(c2.get("CZ1", "other-requester"))
        self.assertIsNone(c2.get("CZ2", "sigA"))
        c2.entries["CZ1"]["cached_epoch"] -= 3 * 86400
        self.assertIsNone(c2.get("CZ1", "sigA"))
        c2.max_age = 0                                           # 0 = never expire
        self.assertIsNotNone(c2.get("CZ1", "sigA"))

    def test_incremental_flush(self):
        c = vc.Cache(self.path)
        for i in range(vc.CACHE_FLUSH_EVERY):
            c.put(f"CZ{i}", "s", {"status": "valid"})
        self.assertTrue(os.path.exists(self.path))               # flushed without explicit flush()

    def test_old_format_discarded(self):
        with open(self.path, "w") as f:
            json.dump({"CZ1": {"status": "valid"}}, f)
        c = vc.Cache(self.path)
        self.assertTrue(c.discarded_old_format)
        self.assertEqual(c.entries, {})

    def test_signature_depends_on_requester_and_query(self):
        a = vc.query_signature("CZ", "1", {})
        self.assertNotEqual(a, vc.query_signature("CZ", "2", {}))
        self.assertNotEqual(a, vc.query_signature("CZ", "1", {"trader_name": "X"}))


class Capabilities(unittest.TestCase):
    def test_capabilities_json_in_sync(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capabilities.json")
        with open(path, encoding="utf-8") as f:
            committed = json.load(f)
        self.assertEqual(committed, json.loads(json.dumps(vc.build_capabilities())),
                         "regenerate: python3 vat_checker.py --capabilities > capabilities.json")

    def test_all_eu_countries_have_format(self):
        self.assertEqual(set(vv.FORMATS), vv.EU_COUNTRY_CODES)
        self.assertEqual(len(vv.EU_COUNTRY_CODES), 28)           # 27 states + XI


class Mz(unittest.TestCase):
    def test_parse_mf_response(self):
        raw = b"""<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
        <StatusNespolehlivyPlatceResponse xmlns="http://adis.mfcr.cz/rozhraniCRPDPH/">
        <status statusCode="0" statusText="OK"/>
        <statusPlatceDPH dic="14890992" nespolehlivyPlatce="NE"><zverejneneUcty>
        <ucet><standardniUcet cislo="1001042725" kodBanky="5500"/></ucet>
        <ucet><nestandardniUcet cislo="IBAN-X"/></ucet></zverejneneUcty></statusPlatceDPH>
        </StatusNespolehlivyPlatceResponse></s:Body></s:Envelope>"""
        out = cz_enrichment.parse_mf_response(raw)
        self.assertEqual(out["14890992"]["unreliable"], "NE")
        self.assertEqual(out["14890992"]["accounts"], ["1001042725/5500", "IBAN-X"])
        bad = raw.replace(b'statusCode="0"', b'statusCode="2"')
        with self.assertRaises(RuntimeError):
            cz_enrichment.parse_mf_response(bad)


class Pipeline(unittest.TestCase):
    """End-to-end with VIES mocked and delays zeroed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.inp = os.path.join(self.dir, "in.csv")
        with open(self.inp, "w", newline="") as f:
            f.write("vat_id,country_code\nCZ14890992,\n12345678,CZ\nCZ45274649,\nDE143266280,\n"
                    "CZ14890992,\nXX999,\n")
        self.patches = [mock.patch.object(vc, n, 0.0) for n in
                        ("BASE_DELAY", "SLOW_BASE_DELAY", "RETRY_BASE_DELAY", "CIRCUIT_BREAKER_BASE_COOLDOWN")]
        for p in self.patches:
            p.start()
        self.lockp = mock.patch.object(vc, "lock_path", lambda: os.path.join(self.dir, "l.lock"))
        self.lockp.start()

    def tearDown(self):
        for p in self.patches + [self.lockp]:
            p.stop()

    def run_main(self, extra=(), vies=None):
        calls = []

        def fake(cc, num, rc, rv, query=None, timeout=0):
            calls.append((cc, num))
            if vies:
                return vies(cc, num, len(calls))
            r = vc.parse_response(OK_XML)
            return r
        out = os.path.join(self.dir, "out.csv")
        with mock.patch.object(vc, "call_vies_once", fake):
            code = vc.main(["--input", self.inp, "--output", out, "--requester-country", "CZ",
                            "--requester-vat", "CZ14890992", *extra])
        with open(out, encoding="utf-8") as f:
            rows = list(csv.DictReader(f)) if out.endswith("csv") and "jsonl" not in extra else \
                [json.loads(l) for l in f]
        return code, rows, calls

    def test_local_rejection_dedupe_and_columns(self):
        code, rows, calls = self.run_main()
        self.assertEqual(code, vc.EXIT_OK)
        statuses = [r["status"] for r in rows]
        self.assertEqual(statuses, ["valid", "invalid_format", "valid", "valid", "valid", "invalid_format"])
        self.assertEqual(sorted(calls), [("CZ", "14890992"), ("CZ", "45274649"), ("DE", "143266280")])
        self.assertEqual(rows[0]["request_identifier"], "WAPIAAAAaHzPoBKL")
        self.assertEqual(rows[4]["from_cache"], "True")           # duplicate served from cache
        self.assertIn("checksum", rows[1]["error_detail"])

    def test_cache_survives_second_run_and_refresh(self):
        self.run_main()
        _, _, calls = self.run_main()
        self.assertEqual(calls, [])
        _, _, calls = self.run_main(["--refresh"])
        self.assertEqual(len(calls), 3)

    def test_error_exit_code_and_second_pass_is_slower(self):
        paces = []
        real = vc.process_country_group

        def spy(cc, items, ctx, progress, pace_mult=1.0):
            paces.append(pace_mult)
            return real(cc, items, ctx, progress, pace_mult)

        def always_fail(cc, num, n):
            raise vc.VatCheckError("INVALID_INPUT")
        with mock.patch.object(vc, "process_country_group", spy):
            code, rows, _ = self.run_main(vies=always_fail)
        self.assertEqual(code, vc.EXIT_ERRORS)
        self.assertEqual(rows[0]["error_detail"], "INVALID_INPUT")
        self.assertIn(vc.SECOND_PASS_PACE_MULT, paces)
        self.assertIn(1.0, paces)

    def test_retry_then_success(self):
        def flaky(cc, num, n):
            if n == 1:
                raise vc.VatCheckError("MS_UNAVAILABLE")
            return vc.parse_response(OK_XML)
        code, rows, _ = self.run_main(vies=flaky)
        self.assertEqual(code, vc.EXIT_OK)

    def test_jsonl_and_summary(self):
        summ = os.path.join(self.dir, "s.json")
        code, rows, _ = self.run_main(["--format", "jsonl", "--summary-json", summ])
        out = os.path.join(self.dir, "out.csv")
        self.assertEqual(rows[0]["status"], "valid")
        s = json.load(open(summ))
        self.assertEqual(s["per_country"]["CZ"]["trader_name_returned"], 3)
        self.assertEqual(s["status_counts"]["invalid_format"], 2)

    def test_interrupt_writes_partial_output(self):
        def boom(cc, num, n):
            raise KeyboardInterrupt
        # KeyboardInterrupt inside a worker thread: simulate main-thread interrupt instead
        with mock.patch.object(vc, "run_pass", side_effect=KeyboardInterrupt):
            code, rows, _ = self.run_main()
        self.assertEqual(code, vc.EXIT_INTERRUPTED)
        self.assertIn("not_checked", [r["status"] for r in rows])
        self.assertIn("invalid_format", [r["status"] for r in rows])   # local results kept

    def test_dry_run_needs_no_requester(self):
        self.assertEqual(vc.main(["--input", self.inp, "--dry-run"]), vc.EXIT_OK)

    def test_no_trailing_delay_after_last_call(self):
        """The pacing wait must happen before a live call, never after the last."""
        waits = []
        real_wait = threading.Event.wait
        with mock.patch.object(vc, "BASE_DELAY", 7.0), \
                mock.patch.object(threading.Event, "wait", lambda self, t=None: waits.append(t) or False):
            self.run_main()
        # 3 live calls over CZ(2) + DE(1): one pre-call wait only (second CZ call)
        self.assertEqual([w for w in waits if w], [7.0])


class Lock(unittest.TestCase):
    def test_lock_exclusive_and_stale_cleared(self):
        d = tempfile.mkdtemp()
        with mock.patch.object(vc, "lock_path", lambda: os.path.join(d, "l")):
            p = vc.acquire_lock()
            with self.assertRaises(RuntimeError):
                vc.acquire_lock()
            vc.release_lock(p)
            with open(os.path.join(d, "l"), "w") as f:
                f.write("999999999")                              # dead pid
            if os.name != "nt":
                vc.release_lock(vc.acquire_lock())


if __name__ == "__main__":
    unittest.main()
