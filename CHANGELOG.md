# Changelog

## 1.0.0 - 2026-11-07

Initial release.

* Reconciles a broker rate confirmation JSON against a carrier
  invoice JSON, matched by load number.
* Line-by-line comparison of linehaul, fuel surcharge, detention,
  lumper, and other accessorials, including named other accessorials
  (tarp fee, pallet fee) compared item by item.
* Flags underbilled amounts, missing accessorials, invoice-only
  charges with no ratecon backing, overbilled lines, loads present
  in only one document, and duplicate load numbers.
* Decimal money math with cents rounding (half up); no float money
  math anywhere.
* Text and --json output, --out file output, --tolerance (default
  $0.01), and a --check CI gate that exits 1 on underbilling over
  tolerance, missing accessorials, duplicate load numbers, or
  invalid data.
* Zero dependencies, Python 3.10 or newer.
