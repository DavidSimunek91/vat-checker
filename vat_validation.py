"""
Local (offline) VAT ID normalisation and validation.

Purpose: catch IDs that cannot possibly be valid BEFORE spending a VIES call
on them. VIES calls are the scarce resource (France alone costs ~10 s per
call), so a typo found locally is a ~10 s saving and a clearer message than
a bare INVALID_INPUT.

Policy: conservative. A local rejection means "this cannot be a VAT ID of
that country" (wrong length/charset, or a checksum that provably fails).
Anything that passes is still sent to VIES, which is the only authority on
whether the ID is actually registered. Checksums are implemented only for
countries where the algorithm is simple and unambiguous (CZ, FR, BE, DK, PL).
Everything else gets a format check only.

No third-party dependencies.
"""

import re

# 27 EU member states as VIES names them (Greece = EL, not GR) plus XI
# (Northern Ireland, goods only, under the Windsor Framework). GB is NOT
# covered by VIES since Brexit.
EU_COUNTRY_CODES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",
}

# Accepted spellings that map onto a VIES country code.
COUNTRY_ALIASES = {"GR": "EL"}

# Format of the national part (everything after the 2-letter prefix).
FORMATS = {
    "AT": r"U\d{8}",
    "BE": r"[01]\d{9}",
    "BG": r"\d{9,10}",
    "CY": r"\d{8}[A-Z]",
    "CZ": r"\d{8,10}",
    "DE": r"\d{9}",
    "DK": r"\d{8}",
    "EE": r"\d{9}",
    "EL": r"\d{9}",
    "ES": r"[A-Z0-9]\d{7}[A-Z0-9]",
    "FI": r"\d{8}",
    "FR": r"[A-Z0-9]{2}\d{9}",
    "HR": r"\d{11}",
    "HU": r"\d{8}",
    "IE": r"(\d{7}[A-Z]{1,2}|\d[A-Z+*]\d{5}[A-Z])",
    "IT": r"\d{11}",
    "LT": r"(\d{9}|\d{12})",
    "LU": r"\d{8}",
    "LV": r"\d{11}",
    "MT": r"\d{8}",
    "NL": r"\d{9}B\d{2}",
    "PL": r"\d{10}",
    "PT": r"\d{9}",
    "RO": r"\d{2,10}",
    "SE": r"\d{12}",
    "SI": r"\d{8}",
    "SK": r"\d{10}",
    "XI": r"(\d{9}|\d{12}|GD\d{3}|HA\d{3})",
}
_COMPILED = {cc: re.compile("^" + pat + "$") for cc, pat in FORMATS.items()}

# Countries where a checksum is verified on top of the format.
CHECKSUM_COUNTRIES = ("BE", "CZ", "DK", "FR", "PL")

_SEPARATORS = re.compile(r"[\s.\-/_,]")


def normalise(raw):
    """Upper-case and strip whitespace and common separators."""
    return _SEPARATORS.sub("", (raw or "")).upper()


def canonical_country(code):
    code = (code or "").strip().upper()
    return COUNTRY_ALIASES.get(code, code)


def split_vat_id(raw_id, country_hint=None):
    """Return (country_code, national_number); country_code may be None.

    A recognised 2-letter prefix in the ID wins. Otherwise the country hint
    (a separate column) is used. NOTE: a bare number is only a usable VAT ID
    if it IS the national VAT number. For CZ legal entities that equals the
    IČO, for CZ sole traders it is the birth number, and in many other
    countries (FR SIREN, DE Steuernummer, ...) it is a different number.
    """
    cleaned = normalise(raw_id)
    m = re.match(r"^([A-Z]{2})([A-Z0-9+*]+)$", cleaned)
    if m:
        prefix = canonical_country(m.group(1))
        if prefix in EU_COUNTRY_CODES:
            return prefix, m.group(2)
    if country_hint and country_hint.strip():
        return canonical_country(country_hint), cleaned
    return None, cleaned


def _digits(s):
    return [int(c) for c in s]


def _check_cz(n):
    # Only 8-digit numbers (legal entities, = IČO) carry a checksum.
    # 9-10 digit numbers are birth numbers / special cases: format only.
    if len(n) != 8:
        return None
    d = _digits(n)
    s = sum(d[i] * (8 - i) for i in range(7))
    r = s % 11
    expected = {0: 1, 1: 0}.get(r, 11 - r)
    if d[7] != expected:
        return "CZ checksum mismatch (8-digit IČO/DIČ)"
    return None


def _check_fr(n):
    # Numeric key only; keys with letters are issued under a different scheme.
    key, siren = n[:2], n[2:]
    if not key.isdigit():
        return None
    if int(key) != (12 + 3 * (int(siren) % 97)) % 97:
        return "FR check key mismatch"
    return None


def _check_be(n):
    if 97 - (int(n[:8]) % 97) != int(n[8:]):
        return "BE mod-97 checksum mismatch"
    return None


def _check_dk(n):
    weights = (2, 7, 6, 5, 4, 3, 2, 1)
    if sum(w * d for w, d in zip(weights, _digits(n))) % 11 != 0:
        return "DK mod-11 checksum mismatch"
    return None


def _check_pl(n):
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    c = sum(w * d for w, d in zip(weights, _digits(n[:9]))) % 11
    if c == 10 or c != int(n[9]):
        return "PL checksum mismatch"
    return None


_CHECKERS = {"CZ": _check_cz, "FR": _check_fr, "BE": _check_be,
             "DK": _check_dk, "PL": _check_pl}


def validate_local(country_code, number):
    """Return None if the ID passes local checks, else a short reason."""
    if not country_code:
        return "no country code (no recognised prefix and no country column)"
    if country_code not in EU_COUNTRY_CODES:
        return f"country '{country_code}' is not covered by VIES"
    if not _COMPILED[country_code].match(number or ""):
        return f"{country_code} number does not match format {FORMATS[country_code]}"
    checker = _CHECKERS.get(country_code)
    return checker(number) if checker else None
