# rate-match-doctor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/aamirtechs/rate-match-doctor/blob/main/LICENSE) [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/aamirtechs/rate-match-doctor/issues)

A zero-dependency Python CLI that reconciles a broker rate
confirmation against the carrier invoice for the same loads. It
matches by load number and compares linehaul, fuel surcharge,
detention, lumper, and other accessorials line by line, so the
shortfall is visible before the invoice goes to the broker.

Every dispatched load ends with an invoice that must match the
signed ratecon, and mismatches are the single biggest cause of
delayed broker payment. This tool catches the usual ways money
leaks: a linehaul billed low, a detention charge left off the
invoice, and a lumper fee with no ratecon backing.

## Install

Requires Python 3.10 or newer. No dependencies beyond the standard
library.

```bash
pip install rate-match-doctor
```

Or run it straight from the source checkout:

```bash
python3 rate_match_doctor.py examples/ratecon.json examples/invoice.json
```

## Input format

Two JSON files: the rate confirmation first, the invoice second.
Each file is a JSON list, an object with a `loads` list, or a
single load object. Each load needs a load number (`load_number`,
also accepted as `load_id`, `load`, or `id`) and any of the five
lines. Missing lines count as zero.

```json
{
  "loads": [
    {"load_number": "48213", "linehaul": 1850.00,
     "fuel_surcharge": 425.50, "detention": 150.00, "lumper": 85.00,
     "other_accessorials": 0}
  ]
}
```

Field aliases are accepted: `line_haul` for linehaul, `fsc` or
`fuel` for fuel surcharge, `detention_pay` for detention,
`lumper_fee` for lumper. Amounts may be numbers or numeric
strings.

Other accessorials can be a plain number or a dict of named items.
When both sides name their items, they are compared item by item,
so a missing tarp fee and an invoice-only pallet fee are separate
findings:

```json
{"load_number": "48214", "linehaul": 2100.00,
 "fuel_surcharge": 462.00, "detention": 225.00, "lumper": 0,
 "other_accessorials": {"tarp fee": 65.00}}
```

## How the matching works

1. Loads are matched by load number. A load on the ratecon with
   no invoice, or invoiced with no ratecon, is an error finding.
2. Each line is compared: invoice minus ratecon, in Decimal cents
   math. A difference within `--tolerance` (default $0.01) is a
   match.
3. An invoice below the ratecon by more than tolerance is
   `underbilled` (error). An accessorial on the ratecon that is
   absent from the invoice is `missing_accessorial` (error).
4. An invoice charge with a zero ratecon line is
   `invoice_only_charge` (warning): money the broker may dispute,
   but not money the carrier lost, so it does not fail `--check`.
   An invoice above a nonzero ratecon line is `overbilled`
   (warning).
5. A load number that appears twice in either file is a
   `duplicate_load` error; only the first entry is compared.

## Usage

A clean match, where the invoice agrees with the ratecon to the
cent:

```bash
python3 rate_match_doctor.py examples/ratecon.json examples/invoice.json
```

Captured output:

```
Load 48213: MATCH
  Linehaul: ratecon $1850.00, invoice $1850.00, match
  Fuel surcharge: ratecon $425.50, invoice $425.50, match
  Detention: ratecon $150.00, invoice $150.00, match
  Lumper: ratecon $85.00, invoice $85.00, match
  Other accessorials: ratecon $0.00, invoice $0.00, match
  totals: ratecon $2510.50, invoice $2510.50, difference $0.00

Loads compared: 1, matched: 1, mismatched: 0, underbilled: $0.00, invoice-only charges: $0.00
Totals: ratecon $2510.50, invoice $2510.50, difference $0.00
```

A mismatch: the invoice bills linehaul $150.00 low, leaves the
$225.00 detention off entirely, and adds a $120.00 lumper fee the
ratecon never agreed to. The tarp fee matches.

```bash
python3 rate_match_doctor.py examples/ratecon-mismatch.json examples/invoice-mismatch.json
```

Captured output:

```
Load 48214: MISMATCH (underbilled $375.00)
  Linehaul: ratecon $2100.00, invoice $1950.00, underbilled
  Fuel surcharge: ratecon $462.00, invoice $462.00, match
  Detention: ratecon $225.00, invoice $0.00, missing accessorial
  Lumper: ratecon $0.00, invoice $120.00, invoice only charge
  Other: tarp fee: ratecon $65.00, invoice $65.00, match
  totals: ratecon $2852.00, invoice $2597.00, difference $-255.00

Findings:
ERROR: load 48214 linehaul underbilled by $150.00 (ratecon $2100.00, invoice $1950.00)
ERROR: load 48214 detention of $225.00 on the ratecon is missing from the invoice
WARNING: load 48214 lumper of $120.00 on the invoice has no ratecon backing

Loads compared: 1, matched: 0, mismatched: 1, underbilled: $375.00, invoice-only charges: $120.00
Totals: ratecon $2852.00, invoice $2597.00, difference $-255.00
```

The invoice total is only $255.00 short because the $120.00
invoice-only lumper charge partly offsets the $375.00 underbilled.
The underbilled figure, not the net difference, is what the
carrier actually lost.

Use it as a CI gate: `--check` exits 1 when any load is
underbilled over tolerance, has a missing accessorial, a
duplicate load number, a load in only one document, or invalid
data. Warnings alone (invoice-only charges, overbilling) do not
fail the gate. On the clean example it exits 0; on the mismatch
example it exits 1.

A duplicate load number fails the gate even when the amounts
match, because only the first entry was compared:

```bash
python3 rate_match_doctor.py examples/ratecon-duplicate.json examples/invoice-duplicate.json --check
```

Captured output:

```
Load 48216: MATCH
  Linehaul: ratecon $1400.00, invoice $1400.00, match
  Fuel surcharge: ratecon $300.00, invoice $300.00, match
  Detention: ratecon $0.00, invoice $0.00, match
  Lumper: ratecon $0.00, invoice $0.00, match
  Other accessorials: ratecon $0.00, invoice $0.00, match
  totals: ratecon $1700.00, invoice $1700.00, difference $0.00

Findings:
ERROR: duplicate load number 48216 in ratecon file; only the first entry was compared

Loads compared: 1, matched: 1, mismatched: 0, underbilled: $0.00, invoice-only charges: $0.00
Totals: ratecon $1700.00, invoice $1700.00, difference $0.00
```

`--json` emits the full report (per-load line records, findings,
errors, and totals) as JSON. The totals section of the mismatch
example:

```json
  "totals": {
    "loads_compared": 1,
    "matched": 0,
    "mismatched": 1,
    "underbilled_total": 375.0,
    "invoice_only_total": 120.0,
    "ratecon_total": 2852.0,
    "invoice_total": 2597.0,
    "difference": -255.0
  }
```

A small rounding difference is not a finding: with
`--tolerance 5.00`, an invoice $5.00 under the ratecon passes
`--check`, and an invoice underbilled by exactly the tolerance
passes too. One cent over the tolerance fails.

## Options

| Flag | Meaning |
| --- | --- |
| `--tolerance` | Ignore differences up to this amount in dollars (default: 0.01) |
| `--json` | Emit JSON instead of text |
| `--check` | Exit 1 on underbilling over tolerance, missing accessorials, duplicate load numbers, loads in only one document, or invalid data |
| `--out` | Write the report to a file |

## Edge cases

* Bad JSON in either file exits 2 with an error on stderr; a
  missing file does the same. See `examples/bad.json`.
* A load entry with no load number is an error for that entry, not
  a crash; the rest of the file still reconciles.
* Negative or non-numeric amounts are errors for that load entry.
* Named other accessorials must match by name to compare item by
  item. If either side gives other accessorials as a plain number,
  the totals are compared as one line instead.
* Money uses Decimal math and rounds to cents half up, so $100.10
  plus $0.20 is exactly $100.30.
* A negative `--tolerance` is rejected with exit 2.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

45 tests, all passing: exact match, underbilled linehaul, missing
detention and lumper accessorials, missing and invoice-only named
other accessorials, overbilling, duplicate load numbers in either
file, loads in only one document, tolerance boundary (exact and
one cent over), custom tolerance, Decimal cents math, string
amounts, field aliases, missing load numbers, negative and
non-numeric amounts, JSON output shape, and `--check` exit codes.

## License

MIT. See [LICENSE](LICENSE).
