#!/usr/bin/env python3
"""Tests for scripts/validate_profiles.py and the Snapmaker 2.0 (A250/A350) preset fixes.

Run:  python3 -I scripts/test_validate_profiles.py [-v]

* Rule unit tests feed small G-code / config snippets to the SM2-* rule functions.
* The bundle tests run the validator on this repo (must PASS) and on a scratch copy with one
  regression re-applied per test (must FAIL with the matching rule ID), so every fix in the
  a250t fix pass stays covered.
* A line-ending test guards the CRLF preset files against accidental LF conversion.
"""

import glob
import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import validate_profiles as vp  # noqa: E402

MACHINE = os.path.join(ROOT, "Snapmaker", "machine")


def raw(name, sub="machine"):
    with open(os.path.join(ROOT, "Snapmaker", sub, name), "rb") as fh:
        return fh.read()


def template(name, key):
    import json
    return json.loads(raw(name).decode("utf-8-sig"))[key]


class RuleUnitTests(unittest.TestCase):
    def test_lim_z(self):
        self.assertEqual([r for r, _ in vp.rule_limits({"machine_max_acceleration_z": ["500"]})], ["SM2-LIM-Z"])
        self.assertEqual(vp.rule_limits({"machine_max_acceleration_z": ["100"]}), [])
        self.assertEqual(vp.rule_limits({"machine_max_acceleration_z": ["3000"],
                                         "emit_machine_limits_to_gcode": "0"}), [])

    def test_lim_xy_is_warning(self):
        rules = [r for r, _ in vp.rule_limits({"machine_max_speed_x": ["150"], "machine_max_speed_y": ["120"]})]
        self.assertEqual(rules, ["warn:SM2-LIM-XY"])

    def test_hotbed_old_block_fails(self):
        old = "G28\nM190 R60\nM83\n{if 1==1 && x >= 90}\nG0 Z0.06\nG92 Z0 ;reset z\n{endif}\nG0 X10 F3420.0\n"
        self.assertEqual([r for r, _ in vp.rule_gc_hotbed(old)], ["SM2-GC-HOTBED"])

    def test_hotbed_new_block_passes(self):
        new = ("G28\nM83\nG0 X10 F3420.0\nG0 Y0 F3420.0\n{if x >= 90}\nG0 Z1.66 F960.0\nG92 Z1.6\n"
               "{else}\nG0 Z1.6 F960.0\n{endif}\n")
        self.assertEqual(vp.rule_gc_hotbed(new), [])

    def test_flush_rule(self):
        old = "M109 S{max(250, min(290, nozzle_temperature_initial_layer[0] + 15))} C2 W1"
        self.assertEqual(len(vp.rule_gc_flush(old, single=True)), 2)
        self.assertEqual(len(vp.rule_gc_flush(old, single=False)), 1)
        dual = "M109 S{min(nozzle_temperature_range_high[0], min(290, max(250, t + 15)))} C2 W1"
        self.assertEqual(vp.rule_gc_flush(dual, single=False), [])
        self.assertEqual(len(vp.rule_gc_flush(dual, single=True)), 1)

    def test_bedmin_issue_552(self):
        self.assertEqual([r for r, _ in vp.rule_gc_bedmin(
            "M140 S{min(bed_temperature[current_extruder], bed_temperature[next_extruder])}")], ["SM2-GC-BEDMIN"])
        self.assertEqual(vp.rule_gc_bedmin(
            "M140 S{max(bed_temperature[current_extruder], bed_temperature[next_extruder])}"), [])

    def test_mcode_allowlist(self):
        self.assertEqual(vp.rule_gc_mcode("M104 S200\n  M109 T1 S200 C3 W1\nM2000 ;ok\n;M9999 comment"), [])
        self.assertEqual([r for r, _ in vp.rule_gc_mcode("M7777 S1")], ["SM2-GC-MCODE"])

    def test_bedtype_label_vs_value(self):
        self.assertEqual([r for r, _ in vp.rule_gc_bedtype('{if curr_bed_type == "Smooth PEI Plate"}')],
                         ["SM2-GC-BEDTYPE"])
        self.assertEqual(vp.rule_gc_bedtype('{if curr_bed_type == "High Temp Plate"}'), [])
        self.assertEqual(vp.rule_gc_bedtype('{if curr_bed_type==\\"Textured PEI Plate\\"}'), [])

    def test_drift_ignores_coordinates_and_comments(self):
        a = "; Model: Snapmaker A250\nG1 X220 E12 F3420.0\nG0 Z0.4 F200\n"
        b = "; Model: Snapmaker A350\n\nG1 X280 E12 F3420.0\nG1 Z0.4 F200 ; same move\n"
        self.assertEqual(vp.rule_gc_drift("a", a, "b", b, "k"), [])
        self.assertEqual([r for r, _ in vp.rule_gc_drift("a", a, "b", b.replace("E12", "E6.264"), "k")],
                         ["SM2-GC-DRIFT"])

    def test_pause_rule(self):
        for bad in (None, "", "  \n", "; M600 in a comment only", ";PAUSE_PRINT", "M0", "G4 S10\nM117 Pause"):
            self.assertEqual([r for r, _ in vp.rule_gc_pause(bad)], ["SM2-GC-PAUSE"], repr(bad))
        for good in ("M600", "M600 ;pause print", "M117 Pause\n  M600\n", "M600 T0"):
            self.assertEqual(vp.rule_gc_pause(good), [], repr(good))

    def test_temp_max(self):
        single = ["Snapmaker A250 (0.4 nozzle)"]
        dual = ["Snapmaker A250 Dual (0.4 nozzle)"]
        self.assertEqual([r for r, _ in vp.rule_temp_max({"nozzle_temperature": ["278"]}, single)], ["SM2-TEMP-MAX"])
        self.assertEqual(vp.rule_temp_max({"nozzle_temperature": ["278"]}, dual), [])
        self.assertEqual([r for r, _ in vp.rule_temp_max({"nozzle_temperature_range_high": ["280"]}, single)],
                         ["warn:SM2-TEMP-MAX"])

    def test_compat_kit(self):
        allp = ["Snapmaker A250 (0.2 nozzle)", "Snapmaker A250 QSKit (0.2 nozzle)", "Snapmaker A250 (0.4 nozzle)"]
        self.assertEqual(vp.printer_family("Snapmaker A250 QS+B Kit (0.4 nozzle)"), (("A250", False, "0.4"), "QS+B Kit"))
        self.assertEqual([r for r, _ in vp.rule_compat_kit(["Snapmaker A250 (0.2 nozzle)"], allp)],
                         ["warn:SM2-COMPAT-KIT"])
        self.assertEqual(vp.rule_compat_kit(allp, allp), [])


class FixedTemplateTests(unittest.TestCase):
    """The shipped A250/A350 templates themselves (fix pass items SP-01, SP-03, #552, SP-06)."""

    BASES = ("fdm_a250.json", "fdm_a350.json", "fdm_a250_dual.json", "fdm_a350_dual.json")

    def test_start_gcode_rules(self):
        for base in self.BASES:
            start = template(base, "machine_start_gcode")
            self.assertEqual(vp.rule_gc_hotbed(start), [], base)
            self.assertEqual(vp.rule_gc_flush(start, single="dual" not in base), [], base)
            self.assertIn("G92 Z1.6", start, base)
            self.assertIn("SM2_HOTBED_COMP", start, base)

    def test_pause_gcode(self):
        """PR-01: Orca's Pause at layer writes machine_pause_gcode; M600 is the SM2 runout pause (single and Dual)."""
        for base in self.BASES:
            self.assertEqual(template(base, "machine_pause_gcode"), "M600", base)
            self.assertEqual(vp.rule_gc_pause(template(base, "machine_pause_gcode")), [], base)

    def test_dual_toolchange(self):
        for base in ("fdm_a250_dual.json", "fdm_a350_dual.json"):
            tc = template(base, "change_filament_gcode")
            self.assertEqual(vp.rule_gc_bedmin(tc), [], base)
            self.assertIn("M140 S{max(bed_temperature[current_extruder], bed_temperature[next_extruder])}", tc)
            self.assertIn("layer [layer_num] z [layer_z]", tc, base)


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.lib = os.path.normpath(os.path.join(ROOT, "..", "OrcaSlicer", "resources", "profiles",
                                                "OrcaFilamentLibrary"))

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def _run(self, root):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):  # keep the per-run SUMMARY lines out of the test output
            return vp.run(["--root", root, "--orca-filament-library", self.lib])

    def test_repo_passes(self):
        rc, report = self._run(ROOT)
        self.assertEqual(rc, 0, report.errors)
        self.assertEqual(report.errors, [])

    def _mutated(self, rel, old, new, rule):
        tmp = tempfile.mkdtemp(prefix="smprof-")
        try:
            shutil.copytree(os.path.join(ROOT, "Snapmaker"), os.path.join(tmp, "Snapmaker"))
            shutil.copy(os.path.join(ROOT, "Snapmaker.json"), tmp)
            path = os.path.join(tmp, "Snapmaker", rel)
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertIn(old, data, "fixture text not found in %s" % rel)
            with open(path, "wb") as fh:
                fh.write(data.replace(old, new, 1))
            rc, report = self._run(tmp)
            self.assertEqual(rc, 1)
            self.assertGreater(report.rule_counts.get(rule, 0), 0, report.errors[:5])
        finally:
            shutil.rmtree(tmp)

    def test_sp02_z_accel_regression(self):
        self._mutated("machine/fdm_linear2.json", b'"machine_max_acceleration_z": [\r\n    "100"',
                      b'"machine_max_acceleration_z": [\r\n    "500"', "SM2-LIM-Z")

    def test_sp04_pet_regression(self):
        self._mutated("filament/Snapmaker PET.json", b'"nozzle_temperature": [\r\n    "275"',
                      b'"nozzle_temperature": [\r\n    "278"', "SM2-TEMP-MAX")

    def test_sp05_dual_default_regression(self):
        self._mutated("machine/fdm_linear2_dual.json", b'"Snapmaker PLA @Dual",\r\n    "Snapmaker PLA @Dual"',
                      b'"Snapmaker PLA",\r\n    "Snapmaker PETG"', "SM2-DEF-COMPAT")

    def test_issue_552_regression(self):
        self._mutated("machine/fdm_a250_dual.json", b"M140 S{max(bed_temperature", b"M140 S{min(bed_temperature",
                      "SM2-GC-BEDMIN")

    def test_drift_regression(self):
        self._mutated("machine/fdm_a350_dual.json", b"G1 X40 E12 F3420.0", b"G1 X40 E6.264 F3420.0",
                      "SM2-GC-DRIFT")

    def test_pr01_pause_empty_regression(self):
        self._mutated("machine/fdm_a250.json", b'"machine_pause_gcode": "M600"', b'"machine_pause_gcode": ""',
                      "SM2-GC-PAUSE")

    def test_pr01_pause_missing_regression(self):
        # Dual base without the key: it then resolves to nothing (fdm_linear2_dual/fdm_linear2/fdm_common lack it)
        self._mutated("machine/fdm_a250_dual.json", b'  "machine_pause_gcode": "M600",\r\n', b"", "SM2-GC-PAUSE")

    def test_pr01_pause_drift_regression(self):
        self._mutated("machine/fdm_a350.json", b'"machine_pause_gcode": "M600"', b'"machine_pause_gcode": "M600\\nM0"',
                      "SM2-GC-DRIFT")

    def test_sp01_hotbed_regression(self):
        self._mutated("machine/fdm_a250.json", b"M83\\n", b"M83\\nG0 Z0.06\\nG92 Z0 ;reset z\\n", "SM2-GC-HOTBED")


class LineEndingTests(unittest.TestCase):
    def test_crlf_files_have_no_bare_lf(self):
        bad = []
        for f in glob.glob(os.path.join(ROOT, "Snapmaker", "**", "*.json"), recursive=True) + \
                [os.path.join(ROOT, "Snapmaker.json")]:
            if "U1" in os.path.basename(f):
                continue  # 12 upstream U1 filament files already mix CRLF/LF on upstream main (787b898, 1e201f6)
            with open(f, "rb") as fh:
                data = fh.read()
            crlf = data.count(b"\r\n")
            if crlf and crlf != data.count(b"\n"):
                bad.append(os.path.relpath(f, ROOT))
        self.assertEqual(bad, [], "mixed line endings (CRLF file with bare LF lines)")

    def test_sm2_bases_are_crlf(self):
        for name in ("fdm_a250.json", "fdm_a350.json", "fdm_a250_dual.json", "fdm_a350_dual.json",
                     "fdm_linear2.json", "fdm_linear2_dual.json"):
            self.assertIn(b"\r\n", raw(name), name)
        with open(os.path.join(ROOT, "Snapmaker.json"), "rb") as fh:
            self.assertIn(b"\r\n", fh.read())


if __name__ == "__main__":
    unittest.main()
