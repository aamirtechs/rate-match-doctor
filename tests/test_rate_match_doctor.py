"""Tests for rate_match_doctor."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal

import rate_match_doctor
from rate_match_doctor import (
    DocError,
    compare_load,
    load_document,
    main,
    normalize_load,
    reconcile,
)


def rc(load_number, linehaul=1000, fuel_surcharge=200, detention=0,
       lumper=0, other=0):
    return {"load_number": load_number, "linehaul": linehaul,
            "fuel_surcharge": fuel_surcharge, "detention": detention,
            "lumper": lumper, "other_accessorials": other}


def inv(load_number, linehaul=1000, fuel_surcharge=200, detention=0,
        lumper=0, other=0):
    return {"load_number": load_number, "linehaul": linehaul,
            "fuel_surcharge": fuel_surcharge, "detention": detention,
            "lumper": lumper, "other_accessorials": other}


class TestReconcile(unittest.TestCase):
    def test_exact_match(self):
        report = reconcile([rc("48213", detention=150, lumper=85)],
                           [inv("48213", detention=150, lumper=85)],
                           Decimal("0.01"))
        self.assertEqual(report["totals"]["matched"], 1)
        self.assertEqual(report["totals"]["mismatched"], 0)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["totals"]["ratecon_total"], 1435.0)
        self.assertEqual(report["totals"]["invoice_total"], 1435.0)

    def test_exact_match_zero_lines(self):
        report = reconcile([rc("1")], [inv("1")], Decimal("0.01"))
        self.assertEqual(report["loads"][0]["status"], "match")
        self.assertEqual(report["totals"]["underbilled_total"], 0.0)

    def test_underbilled_linehaul(self):
        report = reconcile([rc("48214", linehaul=2100)],
                           [inv("48214", linehaul=1950)],
                           Decimal("0.01"))
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["type"], "underbilled")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["difference"], -150.0)
        self.assertEqual(report["totals"]["underbilled_total"], 150.0)
        self.assertEqual(report["loads"][0]["status"], "mismatch")

    def test_missing_detention_accessorial(self):
        report = reconcile([rc("48214", detention=225)],
                           [inv("48214")], Decimal("0.01"))
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["type"], "missing_accessorial")
        self.assertEqual(finding["severity"], "error")
        self.assertIn("missing from the invoice", finding["message"])
        self.assertEqual(report["totals"]["underbilled_total"], 225.0)

    def test_missing_lumper_accessorial(self):
        report = reconcile([rc("5", lumper=90)],
                           [inv("5")], Decimal("0.01"))
        self.assertEqual(report["findings"][0]["type"],
                         "missing_accessorial")
        self.assertEqual(report["findings"][0]["line"], "lumper")

    def test_missing_named_other_accessorial(self):
        report = reconcile(
            [rc("5", other={"tarp fee": 65})],
            [inv("5", other={})], Decimal("0.01"))
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["type"],
                         "missing_accessorial")
        self.assertEqual(report["findings"][0]["line"],
                         "other_accessorials:tarp fee")

    def test_invoice_only_lumper_charge(self):
        report = reconcile([rc("48214")],
                           [inv("48214", lumper=120)], Decimal("0.01"))
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["type"], "invoice_only_charge")
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(report["totals"]["invoice_only_total"], 120.0)
        self.assertEqual(report["totals"]["underbilled_total"], 0.0)

    def test_invoice_only_named_other_accessorial(self):
        report = reconcile(
            [rc("5", other={})],
            [inv("5", other={"pallet fee": 40})], Decimal("0.01"))
        self.assertEqual(report["findings"][0]["type"],
                         "invoice_only_charge")
        self.assertEqual(report["findings"][0]["line"],
                         "other_accessorials:pallet fee")

    def test_overbilled_line_is_warning(self):
        report = reconcile([rc("5", linehaul=1000)],
                           [inv("5", linehaul=1100)], Decimal("0.01"))
        finding = report["findings"][0]
        self.assertEqual(finding["type"], "overbilled")
        self.assertEqual(finding["severity"], "warning")
        self.assertEqual(finding["difference"], 100.0)

    def test_duplicate_load_numbers_in_ratecon(self):
        report = reconcile([rc("48216"), rc("48216", detention=75)],
                           [inv("48216")], Decimal("0.01"))
        self.assertEqual(len(report["loads"]), 1)
        duplicates = [f for f in report["findings"]
                      if f["type"] == "duplicate_load"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["severity"], "error")
        self.assertIn("ratecon", duplicates[0]["message"])
        # First occurrence wins: detention stays 0, no underbilling.
        self.assertEqual(report["loads"][0]["status"], "match")

    def test_duplicate_load_numbers_in_invoice(self):
        report = reconcile([rc("48216")],
                           [inv("48216"), inv("48216", lumper=50)],
                           Decimal("0.01"))
        duplicates = [f for f in report["findings"]
                      if f["type"] == "duplicate_load"]
        self.assertEqual(len(duplicates), 1)
        self.assertIn("invoice", duplicates[0]["message"])

    def test_load_on_ratecon_but_not_invoiced(self):
        report = reconcile([rc("48217"), rc("48218")],
                           [inv("48217")], Decimal("0.01"))
        missing = [f for f in report["findings"]
                   if f["type"] == "load_not_invoiced"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["load_number"], "48218")
        self.assertEqual(missing[0]["severity"], "error")
        self.assertEqual(report["totals"]["loads_compared"], 1)

    def test_load_invoiced_but_no_ratecon(self):
        report = reconcile([rc("48217")],
                           [inv("48217"), inv("48219")], Decimal("0.01"))
        orphan = [f for f in report["findings"]
                  if f["type"] == "load_no_ratecon"]
        self.assertEqual(len(orphan), 1)
        self.assertEqual(orphan[0]["load_number"], "48219")
        self.assertEqual(orphan[0]["severity"], "error")

    def test_tolerance_boundary_exact(self):
        # Underbilled by exactly the tolerance: still a match.
        report = reconcile([rc("5", linehaul=Decimal("1000.00"))],
                           [inv("5", linehaul=Decimal("999.99"))],
                           Decimal("0.01"))
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["loads"][0]["status"], "match")

    def test_tolerance_boundary_one_cent_over(self):
        # Underbilled by one cent more than the tolerance: an error.
        report = reconcile([rc("5", linehaul=Decimal("1000.00"))],
                           [inv("5", linehaul=Decimal("999.98"))],
                           Decimal("0.01"))
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["type"], "underbilled")
        self.assertEqual(report["findings"][0]["difference"], -0.02)

    def test_custom_tolerance_covers_larger_gap(self):
        report = reconcile([rc("5", linehaul=1000)],
                           [inv("5", linehaul=995)],
                           Decimal("5.00"))
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["loads"][0]["status"], "match")

    def test_decimal_cents_math_no_float_error(self):
        # 100.10 plus 0.20 must be exactly 100.30, not 100.30000000000001.
        report = reconcile(
            [rc("5", linehaul=Decimal("100.10"),
                fuel_surcharge=Decimal("0.20"))],
            [inv("5", linehaul=Decimal("100.10"),
                 fuel_surcharge=Decimal("0.20"))],
            Decimal("0.01"))
        self.assertEqual(report["loads"][0]["ratecon_total"], 100.30)
        self.assertEqual(report["loads"][0]["invoice_total"], 100.30)
        self.assertEqual(report["loads"][0]["difference"], 0.0)
        self.assertEqual(report["loads"][0]["status"], "match")
        # A second float trap: 0.10 plus 0.20 is exactly 0.30.
        report = reconcile(
            [rc("6", linehaul=Decimal("0.10"),
                fuel_surcharge=Decimal("0.20"))],
            [inv("6", linehaul=Decimal("0.30"), fuel_surcharge=0)],
            Decimal("0.01"))
        self.assertEqual(report["loads"][0]["ratecon_total"], 0.30)
        self.assertEqual(report["loads"][0]["invoice_total"], 0.30)

    def test_amounts_as_strings(self):
        report = reconcile(
            [{"load_number": "5", "linehaul": "1850.00",
              "fuel_surcharge": "425.50"}],
            [{"load_number": "5", "linehaul": "1850.00",
              "fuel_surcharge": "425.50"}],
            Decimal("0.01"))
        self.assertEqual(report["loads"][0]["status"], "match")
        self.assertEqual(report["loads"][0]["ratecon_total"], 2275.50)

    def test_alias_field_names(self):
        report = reconcile(
            [{"load_number": "5", "line_haul": 1000, "fsc": 200,
              "detention_pay": 50, "lumper_fee": 25}],
            [inv("5", detention=50, lumper=25)],
            Decimal("0.01"))
        self.assertEqual(report["loads"][0]["status"], "match")

    def test_missing_load_number_is_error(self):
        report = reconcile([{"linehaul": 1000}], [inv("5")],
                           Decimal("0.01"))
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("missing a load number", report["errors"][0])
        self.assertEqual(report["loads"], [])

    def test_negative_amount_is_error(self):
        report = reconcile([rc("5", linehaul=-100)], [inv("5")],
                           Decimal("0.01"))
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("negative", report["errors"][0])

    def test_non_numeric_amount_is_error(self):
        report = reconcile([rc("5", linehaul="abc")], [inv("5")],
                           Decimal("0.01"))
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("not a number", report["errors"][0])

    def test_other_as_plain_number_matches_named_total(self):
        report = reconcile([rc("5", other=65)],
                           [inv("5", other={"tarp fee": 65})],
                           Decimal("0.01"))
        self.assertEqual(report["loads"][0]["status"], "match")

    def test_multiple_loads_sorted(self):
        report = reconcile(
            [rc("48220"), rc("48218", linehaul=900)],
            [inv("48220"), inv("48218", linehaul=900)],
            Decimal("0.01"))
        self.assertEqual(
            [item["load_number"] for item in report["loads"]],
            ["48218", "48220"])
        self.assertEqual(report["totals"]["loads_compared"], 2)

    def test_grand_totals(self):
        report = reconcile(
            [rc("1", linehaul=1000), rc("2", linehaul=2000)],
            [inv("1", linehaul=900), inv("2", linehaul=2000)],
            Decimal("0.01"))
        self.assertEqual(report["totals"]["ratecon_total"], 3400.0)
        self.assertEqual(report["totals"]["invoice_total"], 3300.0)
        self.assertEqual(report["totals"]["difference"], -100.0)
        self.assertEqual(report["totals"]["underbilled_total"], 100.0)

    def test_compare_load_unit(self):
        _, lines_rc = normalize_load(rc("9", detention=100), "ratecon")
        _, lines_inv = normalize_load(inv("9"), "invoice")
        records, findings = compare_load(
            "9", lines_rc, lines_inv, Decimal("0.01"))
        detention = [r for r in records if r["line"] == "detention"][0]
        self.assertEqual(detention["status"], "missing_accessorial")
        self.assertEqual(detention["difference"], -100.0)
        self.assertEqual(len(findings), 1)


class TestLoadDocument(unittest.TestCase):
    def write(self, content):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_list_input(self):
        path = self.write('[{"load_number": "1", "linehaul": 5}]')
        self.assertEqual(len(load_document(path)), 1)

    def test_object_with_loads_key(self):
        path = self.write('{"loads": [{"load_number": "1"}]}')
        self.assertEqual(len(load_document(path)), 1)

    def test_single_load_object(self):
        path = self.write('{"load_number": "1", "linehaul": 5}')
        self.assertEqual(len(load_document(path)), 1)

    def test_bad_json_raises(self):
        path = self.write('{ not json')
        with self.assertRaises(DocError):
            load_document(path)

    def test_wrong_shape_raises(self):
        path = self.write('{"broker": "X"}')
        with self.assertRaises(DocError):
            load_document(path)

    def test_missing_file_raises(self):
        with self.assertRaises(DocError):
            load_document("/nonexistent/ratecon.json")


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def write_pair(self, ratecon, invoice):
        rc_path = os.path.join(self.tmpdir.name, "ratecon.json")
        inv_path = os.path.join(self.tmpdir.name, "invoice.json")
        with open(rc_path, "w", encoding="utf-8") as handle:
            json.dump(ratecon, handle, default=str)
        with open(inv_path, "w", encoding="utf-8") as handle:
            json.dump(invoice, handle, default=str)
        return rc_path, inv_path

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_cli_clean_match_exit_zero(self):
        rc_path, inv_path = self.write_pair(
            [rc("48213", detention=150, lumper=85)],
            [inv("48213", detention=150, lumper=85)])
        code, out = self.run_cli([rc_path, inv_path])
        self.assertEqual(code, 0)
        self.assertIn("Load 48213: MATCH", out)
        self.assertIn("ratecon $1435.00, invoice $1435.00", out)

    def test_cli_check_clean_exit_zero(self):
        rc_path, inv_path = self.write_pair([rc("1")], [inv("1")])
        code, _ = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 0)

    def test_cli_check_mismatch_exit_one(self):
        rc_path, inv_path = self.write_pair(
            [rc("48214", linehaul=2100, detention=225)],
            [inv("48214", linehaul=1950)])
        code, out = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 1)
        self.assertIn("underbilled by $150.00", out)
        self.assertIn("missing from the invoice", out)

    def test_cli_mismatch_without_check_exit_zero(self):
        rc_path, inv_path = self.write_pair(
            [rc("48214", linehaul=2100)], [inv("48214", linehaul=1950)])
        code, _ = self.run_cli([rc_path, inv_path])
        self.assertEqual(code, 0)

    def test_cli_check_warning_only_exit_zero(self):
        # An invoice-only charge is a warning, not an underbilling.
        rc_path, inv_path = self.write_pair(
            [rc("5")], [inv("5", lumper=120)])
        code, out = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)

    def test_cli_check_tolerance_boundary(self):
        rc_path, inv_path = self.write_pair(
            [rc("5", linehaul=Decimal("1000.00"))],
            [inv("5", linehaul=Decimal("999.99"))])
        code, _ = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 0)
        rc_path, inv_path = self.write_pair(
            [rc("5", linehaul=Decimal("1000.00"))],
            [inv("5", linehaul=Decimal("999.98"))])
        code, _ = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 1)

    def test_cli_custom_tolerance(self):
        rc_path, inv_path = self.write_pair(
            [rc("5", linehaul=1000)], [inv("5", linehaul=995)])
        code, _ = self.run_cli(
            [rc_path, inv_path, "--check", "--tolerance", "5.00"])
        self.assertEqual(code, 0)

    def test_cli_json_output(self):
        rc_path, inv_path = self.write_pair(
            [rc("48214", linehaul=2100)], [inv("48214", linehaul=1950)])
        code, out = self.run_cli([rc_path, inv_path, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["totals"]["underbilled_total"], 150.0)
        self.assertEqual(report["findings"][0]["type"], "underbilled")
        self.assertEqual(report["loads"][0]["load_number"], "48214")

    def test_cli_bad_json_exit_two(self):
        bad_path = os.path.join(self.tmpdir.name, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as handle:
            handle.write("{ nope")
        rc_path, inv_path = self.write_pair([rc("1")], [inv("1")])
        code, _ = self.run_cli([bad_path, inv_path])
        self.assertEqual(code, 2)

    def test_cli_missing_file_exit_two(self):
        rc_path, inv_path = self.write_pair([rc("1")], [inv("1")])
        code, _ = self.run_cli([rc_path, "/nonexistent/invoice.json"])
        self.assertEqual(code, 2)

    def test_cli_negative_tolerance_exit_two(self):
        rc_path, inv_path = self.write_pair([rc("1")], [inv("1")])
        code, _ = self.run_cli(
            [rc_path, inv_path, "--tolerance", "-1"])
        self.assertEqual(code, 2)

    def test_cli_duplicate_fails_check(self):
        rc_path, inv_path = self.write_pair(
            [rc("48216"), rc("48216", detention=75)], [inv("48216")])
        code, out = self.run_cli([rc_path, inv_path, "--check"])
        self.assertEqual(code, 1)
        self.assertIn("duplicate load number 48216", out)

    def test_cli_out_file(self):
        rc_path, inv_path = self.write_pair([rc("1")], [inv("1")])
        out_path = os.path.join(self.tmpdir.name, "report.txt")
        code, out = self.run_cli([rc_path, inv_path, "--out", out_path])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        with open(out_path, encoding="utf-8") as handle:
            self.assertIn("Load 1: MATCH", handle.read())


if __name__ == "__main__":
    unittest.main()
