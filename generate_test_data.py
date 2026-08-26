#!/usr/bin/env python3
"""
One-off generator for synthetic test data for vat_checker.py.

Produces plausible-*format* VAT IDs (correct length/character pattern per
country; FR numbers even carry a correctly-computed check digit) but they
are NOT real registered companies. Point of this file is to load-test the
pipeline (throughput, retry/backoff, the France slow-down, CSV I/O) - not
to produce a real accounting dataset. Expect most/all rows to come back
"valid: False" since the numbers don't belong to anyone.

Usage:
    python3 generate_test_data.py --out vat_ids_test_1000.csv --count 1000
"""

import argparse
import random

# (country_code, share_of_total) - FR gets 1/3, CZ and BE get a solid slice,
# the rest is spread evenly across the remaining EU members.
FIXED_SHARES = {"FR": 1 / 3, "CZ": 0.15, "BE": 0.10}
OTHER_COUNTRIES = [
    "AT", "BG", "CY", "DE", "DK", "EE", "EL", "ES", "FI", "HR", "HU", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
]


def rand_digits(n):
    return "".join(random.choice("0123456789") for _ in range(n))


def rand_letters(n):
    return "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(n))


def gen_fr():
    # Real FR algorithm: key = (12 + 3 * (siren mod 97)) mod 97
    siren = random.randint(100_000_000, 999_999_999)
    key = (12 + 3 * (siren % 97)) % 97
    return f"FR{key:02d}{siren:09d}"


def gen_cz():
    return "CZ" + rand_digits(8)


def gen_be():
    # BE numbers are 10 digits, historically starting with 0 or 1
    return "BE" + random.choice("01") + rand_digits(9)


GENERATORS = {
    "FR": gen_fr,
    "CZ": gen_cz,
    "BE": gen_be,
    "AT": lambda: "ATU" + rand_digits(8),
    "BG": lambda: "BG" + rand_digits(random.choice([9, 10])),
    "CY": lambda: "CY" + rand_digits(8) + rand_letters(1),
    "DE": lambda: "DE" + rand_digits(9),
    "DK": lambda: "DK" + rand_digits(8),
    "EE": lambda: "EE" + rand_digits(9),
    "EL": lambda: "EL" + rand_digits(9),
    "ES": lambda: "ES" + rand_letters(1) + rand_digits(7) + rand_letters(1),
    "FI": lambda: "FI" + rand_digits(8),
    "HR": lambda: "HR" + rand_digits(11),
    "HU": lambda: "HU" + rand_digits(8),
    "IE": lambda: "IE" + rand_digits(7) + rand_letters(1),
    "IT": lambda: "IT" + rand_digits(11),
    "LT": lambda: "LT" + rand_digits(9),
    "LU": lambda: "LU" + rand_digits(8),
    "LV": lambda: "LV" + rand_digits(11),
    "MT": lambda: "MT" + rand_digits(8),
    "NL": lambda: "NL" + rand_digits(9) + "B" + rand_digits(2),
    "PL": lambda: "PL" + rand_digits(10),
    "PT": lambda: "PT" + rand_digits(9),
    "RO": lambda: "RO" + rand_digits(random.randint(6, 10)),
    "SE": lambda: "SE" + rand_digits(12),
    "SI": lambda: "SI" + rand_digits(8),
    "SK": lambda: "SK" + rand_digits(10),
}


def build_country_list(count):
    countries = []
    for cc, share in FIXED_SHARES.items():
        countries += [cc] * round(count * share)
    remaining = count - len(countries)
    for i in range(remaining):
        countries.append(OTHER_COUNTRIES[i % len(OTHER_COUNTRIES)])
    random.shuffle(countries)
    return countries[:count]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="vat_ids_test_1000.csv")
    parser.add_argument("--count", type=int, default=1000)
    args = parser.parse_args()

    countries = build_country_list(args.count)
    seen = set()
    rows = []
    for cc in countries:
        for _ in range(20):  # avoid rare collisions
            vat_id = GENERATORS[cc]()
            if vat_id not in seen:
                seen.add(vat_id)
                rows.append(vat_id)
                break

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("vat_id\n")
        for vat_id in rows:
            f.write(vat_id + "\n")

    from collections import Counter
    counts = Counter(r[:2] for r in rows)
    print(f"Wrote {len(rows)} synthetic VAT IDs to {args.out}")
    for cc, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cc}: {n}")


if __name__ == "__main__":
    main()
