"""Rate confirmation vs invoice reconciler for truck loads.

Reads a broker rate confirmation JSON and a carrier invoice JSON for
the same loads, matches them by load number, and compares linehaul,
fuel surcharge, detention, lumper, and other accessorials line by
line. Flags underbilled amounts, missing accessorials, invoice-only
charges with no ratecon backing, and duplicate load numbers.

Zero dependencies, Python 3.10 or newer, standard library only.
Money uses Decimal cents math throughout, never float arithmetic.

Usage:
    python3 rate_match_doctor.py examples/ratecon.json examples/invoice.json
    python3 rate_match_doctor.py ratecon.json invoice.json --json --check
"""

import argparse
import json
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

__version__ = "1.0.0"

CENT = Decimal("0.01")
DEFAULT_TOLERANCE = Decimal("0.01")

# (key, label, is_accessorial). Accessorial lines missing from the
# invoice are reported as missing_accessorial; linehaul and fuel
# surcharge missing from the invoice are plain underbilling.
LINES = [
    ("linehaul", "Linehaul", False),
    ("fuel_surcharge", "Fuel surcharge", False),
    ("detention", "Detention", True),
    ("lumper", "Lumper", True),
    ("other_accessorials", "Other accessorials", True),
]

ALIASES = {
    "linehaul": ("line_haul", "linehaul_amount"),
    "fuel_surcharge": ("fsc", "fuel", "fuel_surcharge_amount"),
    "detention": ("detention_pay", "detention_amount"),
    "lumper": ("lumper_fee", "lumper_amount"),
    "other_accessorials": ("other", "accessorials", "other_accessorial"),
}

LOAD_NUMBER_KEYS = ("load_number", "load_id", "load", "id")


class DocError(Exception):
    """A document or load entry that cannot be reconciled."""


def to_decimal(value, field):
    """Convert a JSON number or numeric string to Decimal."""
    if isinstance(value, bool):
        raise DocError("%s is not a number: %r" % (field, value))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DocError("%s is not a number: %r" % (field, value))
    if result.is_nan() or result.is_infinite():
        raise DocError("%s is not a finite number: %r" % (field, value))
    return result


def money(value):
    """Quantize a Decimal to cents, half up."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw, field, load_number):
    """Parse one amount, rejecting non-numbers and negatives."""
    if raw is None:
        return Decimal("0")
    amount = to_decimal(raw, "%s for load %s" % (field, load_number))
    if amount < 0:
        raise DocError(
            "%s for load %s is negative: %s" % (field, load_number, raw))
    return money(amount)


def parse_other_accessorials(raw, load_number):
    """Parse the other accessorials field.

    Returns (items, named) where items maps a name to a Decimal and
    named is True when the input was a dict of named items. A plain
    number becomes a single item named "other".
    """
    if raw is None:
        return {}, False
    if isinstance(raw, dict):
        items = {}
        for name, value in raw.items():
            items[str(name)] = parse_amount(
                value, "other accessorial %r" % name, load_number)
        return items, True
    return {"other": parse_amount(raw, "other_accessorials", load_number)}, False


def load_number_of(raw):
    """Return the load number of a raw load entry, or None."""
    for key in LOAD_NUMBER_KEYS:
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def normalize_load(raw, source):
    """Normalize one raw load entry.

    Returns (load_number, lines) where lines maps each line key to a
    Decimal, except other_accessorials which maps to a dict of named
    items. Raises DocError on bad data. The returned load also
    carries "_other_named" for the comparison step.
    """
    if not isinstance(raw, dict):
        raise DocError("%s: load entry is not an object: %r" % (source, raw))
    load_number = load_number_of(raw)
    if load_number is None:
        raise DocError("%s: load entry is missing a load number" % source)
    lines = {}
    for key, _label, _accessorial in LINES:
        if key == "other_accessorials":
            continue
        value = None
        for candidate in (key,) + ALIASES[key]:
            if raw.get(candidate) is not None:
                value = raw[candidate]
                break
        lines[key] = parse_amount(value, key, load_number)
    other_raw = None
    for candidate in ("other_accessorials",) + ALIASES["other_accessorials"]:
        if raw.get(candidate) is not None:
            other_raw = raw[candidate]
            break
    items, named = parse_other_accessorials(other_raw, load_number)
    lines["other_accessorials"] = items
    lines["_other_named"] = named
    return load_number, lines


def load_document(path):
    """Load a JSON document. Returns a list of raw load entries.

    Accepts a list of loads, an object with a loads/entries/data
    list, or a single load object. Raises DocError on bad JSON or a
    wrong top-level shape.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise DocError("cannot read %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise DocError("invalid JSON in %s: %s" % (path, exc))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("loads", "entries", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        if load_number_of(data) is not None:
            return [data]
    raise DocError(
        "%s must be a JSON list, an object with a loads list, "
        "or a single load object" % path)


def index_loads(raw_entries, source, errors, findings):
    """Normalize entries and index by load number.

    Duplicates are recorded as error findings and only the first
    occurrence is kept. Bad entries go to errors. Returns the
    index dict of load number to normalized lines.
    """
    index = {}
    for raw in raw_entries:
        try:
            load_number, lines = normalize_load(raw, source)
        except DocError as exc:
            errors.append(str(exc))
            continue
        if load_number in index:
            findings.append({
                "load_number": load_number,
                "line": None,
                "type": "duplicate_load",
                "severity": "error",
                "ratecon_amount": None,
                "invoice_amount": None,
                "difference": None,
                "message": "duplicate load number %s in %s file; "
                "only the first entry was compared"
                % (load_number, source),
            })
            continue
        index[load_number] = lines
    return index


def compare_other(ratecon_items, invoice_items, ratecon_named,
                   invoice_named):
    """Compare the other accessorials of one load.

    When both sides name their items, compare per name so a missing
    tarp fee and an invoice-only pallet fee are separate findings.
    Otherwise compare the totals as a single line. Returns a list of
    (key, label, ratecon_amount, invoice_amount) tuples.
    """
    if ratecon_named and invoice_named:
        names = sorted(set(ratecon_items) | set(invoice_items))
        return [("other_accessorials:%s" % name, "Other: %s" % name,
                 ratecon_items.get(name, Decimal("0")),
                 invoice_items.get(name, Decimal("0")))
                for name in names]
    ratecon_total = sum(ratecon_items.values(), Decimal("0"))
    invoice_total = sum(invoice_items.values(), Decimal("0"))
    return [("other_accessorials", "Other accessorials",
             ratecon_total, invoice_total)]


def compare_load(load_number, ratecon_lines, invoice_lines, tolerance):
    """Compare one matched load line by line.

    Returns (line_records, findings). Each line record carries the
    amounts, the difference (invoice minus ratecon), and a status:
    match, underbilled, overbilled, missing_accessorial, or
    invoice_only_charge.
    """
    line_records = []
    findings = []
    comparisons = []
    for key, label, _accessorial in LINES:
        if key == "other_accessorials":
            comparisons.extend(compare_other(
                ratecon_lines[key], invoice_lines[key],
                ratecon_lines["_other_named"],
                invoice_lines["_other_named"]))
        else:
            comparisons.append(
                (key, label, ratecon_lines[key], invoice_lines[key]))
    for key, label, ratecon_amount, invoice_amount in comparisons:
        difference = money(invoice_amount - ratecon_amount)
        is_accessorial = key.startswith("detention") or \
            key.startswith("lumper") or key.startswith("other_accessorials")
        if abs(difference) <= tolerance:
            status = "match"
        elif difference < 0 and invoice_amount == 0 and is_accessorial:
            status = "missing_accessorial"
        elif difference < 0:
            status = "underbilled"
        elif ratecon_amount == 0:
            status = "invoice_only_charge"
        else:
            status = "overbilled"
        line_records.append({
            "line": key,
            "label": label,
            "ratecon_amount": float(ratecon_amount),
            "invoice_amount": float(invoice_amount),
            "difference": float(difference),
            "status": status,
        })
        if status == "match":
            continue
        severity = "error" if status in (
            "underbilled", "missing_accessorial") else "warning"
        if status == "underbilled":
            message = ("load %s %s underbilled by $%.2f "
                       "(ratecon $%.2f, invoice $%.2f)"
                       % (load_number, label.lower(), -difference,
                          ratecon_amount, invoice_amount))
        elif status == "missing_accessorial":
            message = ("load %s %s of $%.2f on the ratecon is missing "
                       "from the invoice"
                       % (load_number, label.lower(), ratecon_amount))
        elif status == "invoice_only_charge":
            message = ("load %s %s of $%.2f on the invoice has no "
                       "ratecon backing"
                       % (load_number, label.lower(), invoice_amount))
        else:
            message = ("load %s %s overbilled by $%.2f "
                       "(ratecon $%.2f, invoice $%.2f)"
                       % (load_number, label.lower(), difference,
                          ratecon_amount, invoice_amount))
        findings.append({
            "load_number": load_number,
            "line": key,
            "type": status,
            "severity": severity,
            "ratecon_amount": float(ratecon_amount),
            "invoice_amount": float(invoice_amount),
            "difference": float(difference),
            "message": message,
        })
    return line_records, findings


def load_total(lines):
    """Total of a normalized load's lines."""
    total = sum(
        (amount for key, amount in lines.items()
         if key in ("linehaul", "fuel_surcharge", "detention", "lumper")),
        Decimal("0"))
    total += sum(lines["other_accessorials"].values(), Decimal("0"))
    return money(total)


def reconcile(ratecon_entries, invoice_entries, tolerance):
    """Reconcile two lists of raw load entries. Returns the report."""
    errors = []
    findings = []
    ratecon_index = index_loads(
        ratecon_entries, "ratecon", errors, findings)
    invoice_index = index_loads(
        invoice_entries, "invoice", errors, findings)

    loads = []
    for load_number in sorted(ratecon_index):
        ratecon_lines = ratecon_index[load_number]
        if load_number not in invoice_index:
            findings.append({
                "load_number": load_number,
                "line": None,
                "type": "load_not_invoiced",
                "severity": "error",
                "ratecon_amount": float(load_total(ratecon_lines)),
                "invoice_amount": None,
                "difference": None,
                "message": "load %s is on the ratecon ($%.2f) but has "
                "no invoice" % (load_number, load_total(ratecon_lines)),
            })
            continue
        invoice_lines = invoice_index[load_number]
        line_records, load_findings = compare_load(
            load_number, ratecon_lines, invoice_lines, tolerance)
        findings.extend(load_findings)
        ratecon_total = load_total(ratecon_lines)
        invoice_total = load_total(invoice_lines)
        loads.append({
            "load_number": load_number,
            "status": "match" if not load_findings else "mismatch",
            "lines": line_records,
            "ratecon_total": float(ratecon_total),
            "invoice_total": float(invoice_total),
            "difference": float(money(invoice_total - ratecon_total)),
        })
    for load_number in sorted(invoice_index):
        if load_number in ratecon_index:
            continue
        invoice_lines = invoice_index[load_number]
        findings.append({
            "load_number": load_number,
            "line": None,
            "type": "load_no_ratecon",
            "severity": "error",
            "ratecon_amount": None,
            "invoice_amount": float(load_total(invoice_lines)),
            "difference": None,
            "message": "load %s is invoiced ($%.2f) but has no ratecon"
            % (load_number, load_total(invoice_lines)),
        })

    underbilled_total = money(sum(
        (Decimal(str(finding["difference"])) * Decimal("-1")
         for finding in findings
         if finding["type"] in ("underbilled", "missing_accessorial")),
        Decimal("0")))
    if underbilled_total == 0:
        underbilled_total = Decimal("0.00")
    invoice_only_total = money(sum(
        (Decimal(str(finding["invoice_amount"]))
         for finding in findings
         if finding["type"] == "invoice_only_charge"),
        Decimal("0")))
    ratecon_grand = money(sum(
        (Decimal(str(item["ratecon_total"])) for item in loads),
        Decimal("0")))
    invoice_grand = money(sum(
        (Decimal(str(item["invoice_total"])) for item in loads),
        Decimal("0")))
    report = {
        "loads": loads,
        "findings": findings,
        "errors": errors,
        "totals": {
            "loads_compared": len(loads),
            "matched": sum(
                1 for item in loads if item["status"] == "match"),
            "mismatched": sum(
                1 for item in loads if item["status"] == "mismatch"),
            "underbilled_total": float(underbilled_total),
            "invoice_only_total": float(invoice_only_total),
            "ratecon_total": float(ratecon_grand),
            "invoice_total": float(invoice_grand),
            "difference": float(money(invoice_grand - ratecon_grand)),
        },
    }
    return report


def format_text(report):
    """Render the report as plain text."""
    lines = []
    for item in report["loads"]:
        if item["status"] == "match":
            lines.append("Load %s: MATCH" % item["load_number"])
        else:
            underbilled = sum(
                -line["difference"] for line in item["lines"]
                if line["status"] in ("underbilled", "missing_accessorial"))
            lines.append("Load %s: MISMATCH (underbilled $%.2f)"
                         % (item["load_number"], underbilled))
        for line in item["lines"]:
            lines.append(
                "  %s: ratecon $%.2f, invoice $%.2f, %s"
                % (line["label"], line["ratecon_amount"],
                   line["invoice_amount"],
                   line["status"].replace("_", " ")))
        lines.append(
            "  totals: ratecon $%.2f, invoice $%.2f, difference $%.2f"
            % (item["ratecon_total"], item["invoice_total"],
               item["difference"]))
        lines.append("")
    if report["findings"]:
        lines.append("Findings:")
        for finding in report["findings"]:
            lines.append("%s: %s" % (
                finding["severity"].upper(), finding["message"]))
        lines.append("")
    totals = report["totals"]
    lines.append(
        "Loads compared: %d, matched: %d, mismatched: %d, "
        "underbilled: $%.2f, invoice-only charges: $%.2f"
        % (totals["loads_compared"], totals["matched"],
           totals["mismatched"], totals["underbilled_total"],
           totals["invoice_only_total"]))
    lines.append(
        "Totals: ratecon $%.2f, invoice $%.2f, difference $%.2f"
        % (totals["ratecon_total"], totals["invoice_total"],
           totals["difference"]))
    for error in report["errors"]:
        lines.append("ERROR: %s" % error)
    return "\n".join(lines) + "\n"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rate-match-doctor",
        description="Reconcile a broker rate confirmation against "
        "the carrier invoice for the same loads, line by line.")
    parser.add_argument("ratecon", help="rate confirmation JSON file")
    parser.add_argument("invoice", help="carrier invoice JSON file")
    parser.add_argument("--tolerance", type=Decimal,
                        default=DEFAULT_TOLERANCE,
                        help="ignore differences up to this amount in "
                        "dollars (default: 0.01)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of text")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if any load is underbilled over "
                        "tolerance, has a missing accessorial, a "
                        "duplicate load number, or invalid data")
    parser.add_argument("--out", default=None,
                        help="write the report to a file")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tolerance < 0:
        print("ERROR: --tolerance must not be negative", file=sys.stderr)
        return 2
    try:
        ratecon_entries = load_document(args.ratecon)
        invoice_entries = load_document(args.invoice)
    except DocError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    report = reconcile(ratecon_entries, invoice_entries, args.tolerance)
    if args.json:
        text = json.dumps(report, indent=2) + "\n"
    else:
        text = format_text(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")
    if args.check and (
            report["errors"]
            or any(finding["severity"] == "error"
                   for finding in report["findings"])):
        return 1
    return 0


def main_entry():
    sys.exit(main())


if __name__ == "__main__":
    main_entry()
