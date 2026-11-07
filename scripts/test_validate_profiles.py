#!/usr/bin/env python3
"""Tests for scripts/validate_profiles.py and the Snapmaker 2.0 (A250/A350) preset fixes.

Run:  python3 -I scripts/test_validate_profiles.py [-v]

* Rule unit tests feed small G-code / config snippets to the SM2-* rule functions.
* The bundle tests run the validator on this repo (must PASS) and on a scratch copy with one
  regression re-applied per test (must FAIL with the matching rule ID), so every fix in the
  a250t fix pass stays covered.
* A line-ending test guards the CRLF preset files against accidental LF conversion.
* Renderer tests pin the Orca PlaceholderParser semantics that render_start_gcode.py mirrors, and purge
  tests render the SM2 start G-code for concrete footprints (PR-09).
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
import render_start_gcode as rsg  # noqa: E402

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


class RendererTests(unittest.TestCase):
    """render_start_gcode.py must follow OrcaSlicer src/libslic3r/PlaceholderParser.cpp where the SM2 templates rely on it."""

    def r(self, text, **variables):
        return rsg.render(text, variables)[0]

    def test_arithmetic_types(self):
        self.assertEqual(self.r("{7/2} {7.0/2} {-7/2} {7%3} {1.0/3}"), "3 3.5 -3 1 0.333333")  # int division truncates
        self.assertEqual(self.r("{min(3, 4)} {max(3, 4.0)} {min(2.5, 3)}"), "3 4 2.5")      # %g output, int stays int
        self.assertEqual(self.r("{digits(3.14159, 0, 2)} {digits(2.5, 0)} {int(2.9)} {round(-2.5)}"), "3.14 3 2 -3")

    def test_vectors_and_legacy(self):
        self.assertEqual(self.r("{v[1]} {v[5]} [i] [s]", v=[1.5, 2.5], i=1, s="x"), "2.5 1.5 1 x")  # out of range -> [0]
        with self.assertRaises(rsg.RenderError):
            self.r("{v}", v=[1.0])
        with self.assertRaises(rsg.RenderError):
            self.r("; [ not a variable")  # Orca: '[' in text must be a legacy expansion

    def test_if_blocks_and_locals(self):
        tpl = '{local a = 2.0; local s = a > 1 ? "big" : "small"}{if s == "big"}B{a * 2}{elsif a > 0}P{else}N{endif}'
        self.assertEqual(self.r(tpl), "B4")
        self.assertEqual(self.r("{if false}{1/0}{endif}ok"), "ok")  # skipped branches are not evaluated
        with self.assertRaises(rsg.RenderError):
            self.r("{if false}{local a = 1}{endif}{a}")  # a local declared in a skipped branch does not exist
        with self.assertRaises(rsg.RenderError):
            self.r("{local a = (1 + 2) * 3}")  # Orca: leading '(' is a vector initializer
        with self.assertRaises(rsg.RenderError):
            self.r("{local nozzle_diameter = 1}", nozzle_diameter=[0.4])  # read-only system symbol
        with self.assertRaises(rsg.RenderError):
            self.r("{if 1}x{endif}")  # not a boolean

    def test_current_templates_render(self):
        for base in FixedTemplateTests.BASES:
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), base[:-5])
            for key in ("machine_start_gcode",):  # end/toolchange templates need layer and toolchange data
                text, _ = rsg.render(cfg[key], rsg.sample_variables(cfg, (90, 100, 140, 150)))
                self.assertNotIn("{", text, (base, key))


class PurgeTests(unittest.TestCase):
    """PR-09: footprint-, nozzle- and MVS-aware purge in the four SM2 start templates."""

    def render(self, printer, footprint, ie=0, mvs=15.0):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
        text, local = rsg.render(cfg["machine_start_gcode"], rsg.sample_variables(cfg, footprint, ie, mvs), ie)
        purge = [m["line"] for m in rsg.gcode_moves(text) if m["line"].startswith(("G0 X", "G1 X"))
                 and "X10 F" not in m["line"]]
        return text, local, purge

    def test_centre_part_purges_in_front(self):
        text, local, purge = self.render("Snapmaker A250 (0.4 nozzle)", (90, 100, 140, 150))
        self.assertIn("; SM2_PURGE front start X90.00 Y85.00 len 100", text)
        self.assertEqual((local["sm2_purge_x"], local["sm2_purge_y"]), (90.0, 85.0))
        self.assertEqual(purge, ["G0 X90.00 Y85.00 F3420.0", "G1 X190.00 Y85.00 E6.2363 F3420",
                                 "G1 X190.00 Y90.00 E0.3118 F200", "G1 X90.00 Y90.00 E6.2363 F3420",
                                 "G0 X90.00 Y95.00 F3420.0"])

    def test_corner_parts_purge_beside(self):
        _, local, purge = self.render("Snapmaker A250 (0.4 nozzle)", (0, 0, 30, 30))
        self.assertEqual((local["sm2_purge_side"], local["sm2_purge_x"], local["sm2_purge_y"]), ("right", 45.0, 0.0))
        self.assertEqual(purge[1], "G1 X45.00 Y100.00 E6.2363 F3420")
        _, local, _ = self.render("Snapmaker A250 (0.4 nozzle)", (200, 0, 230, 30))
        self.assertEqual((local["sm2_purge_side"], local["sm2_purge_x"]), ("left", 185.0))
        _, local, _ = self.render("Snapmaker A250 (0.4 nozzle)", (180, 200, 230, 250))  # back-right: front, shifted left
        self.assertEqual((local["sm2_purge_side"], local["sm2_purge_x"], local["sm2_purge_y"]), ("front", 120.0, 185.0))

    def test_no_room_falls_back_to_old_line(self):
        _, local, purge = self.render("Snapmaker A250 (0.4 nozzle)", (5, 5, 225, 245))
        self.assertEqual(local["sm2_purge_side"], "fallback")
        self.assertEqual(purge, ["G0 X40.00 Y0.00 F3420.0", "G1 X220.00 Y0.00 E11.2253 F3420",
                                 "G1 X220.00 Y10.00 E0.6236 F200", "G1 X40.00 Y10.00 E11.2253 F3420",
                                 "G0 X40.00 Y20.00 F3420.0"])

    def test_nozzle_and_mvs_scale_the_purge(self):
        text, _, purge = self.render("Snapmaker A250 (0.2 nozzle)", (90, 100, 140, 150), mvs=2.0)
        self.assertIn("G1 X190.00 Y85.00 E3.1181 F1600", purge)  # width 0.25, 2 mm3/s / (0.25 x 0.3) = 26.7 mm/s
        self.assertIn("G1 E3 F49.9", text)                         # stationary primes capped by MVS too
        self.assertIn("G1 E4 F49.9", text)
        _, _, purge = self.render("Snapmaker A250 (0.8 nozzle)", (90, 100, 140, 150), mvs=0.0)
        self.assertIn("G1 X190.00 Y85.00 E12.4726 F3420", purge)   # MVS 0 = unset: old 3420 feed

    def test_dual_uses_the_initial_tool(self):
        _, local, purge = self.render("Snapmaker A250 Dual (0.4 nozzle)", (90, 100, 140, 150), ie=1, mvs=[15.0, 2.0])
        self.assertIn("G1 X190.00 Y85.00 E6.2363 F800", purge)     # T1's filament MVS 2 at width 0.5
        self.assertEqual(local["sm2_purge_side"], "front")

    def test_twins_share_the_purge_block(self):
        for a, b in (("fdm_a250.json", "fdm_a350.json"), ("fdm_a250_dual.json", "fdm_a350_dual.json")):
            pa = [l for l in template(a, "machine_start_gcode").split("\n") if "sm2_" in l or "SM2_PURGE" in l]
            pb = [l for l in template(b, "machine_start_gcode").split("\n") if "sm2_" in l or "SM2_PURGE" in l]
            self.assertEqual(pa, pb)
            self.assertGreater(len(pa), 10)

    def test_rule_passes_on_every_base(self):
        for printer in ("Snapmaker A250 (0.4 nozzle)", "Snapmaker A250 Dual QS+B Kit (0.2 nozzle)",
                        "Snapmaker A350 QSKit (0.8 nozzle)"):
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
            self.assertEqual(vp.rule_gc_purge(cfg["machine_start_gcode"], cfg), [], printer)

    def test_rule_catches_fixed_purge(self):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), "Snapmaker A250 (0.4 nozzle)")
        old = "G0 X40 F3420.0\nG0 Z0.3 F960.0\nG1 X220 E12 F3420.0\n"
        self.assertEqual([r for r, _ in vp.rule_gc_purge(old, cfg)], ["SM2-GC-PURGE"])


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
        self._mutated("machine/fdm_a350_dual.json", b"G1 E4 F{digits(min(80.0", b"G1 E6 F{digits(min(80.0",
                      "SM2-GC-DRIFT")

    def test_pr09_purge_placeholder_regression(self):
        self._mutated("machine/fdm_a250.json", b"local sm2_mvs = filament_max_volumetric_speed[initial_extruder]",
                      b"local sm2_mvs = 15.0", "SM2-GC-PURGE")

    def test_pr09_purge_off_bed_regression(self):
        self._mutated("machine/fdm_a250_dual.json", b"local sm2_bx0 = print_bed_min[0] + 10.0",
                      b"local sm2_bx0 = print_bed_min[0] - 30.0", "SM2-GC-PURGE")

    def test_pr09_purge_clearance_regression(self):
        self._mutated("machine/fdm_a350.json", b"local sm2_purge_off = 15.0", b"local sm2_purge_off = 3.0",
                      "SM2-GC-PURGE")

    def test_pr09_purge_flow_regression(self):
        self._mutated("machine/fdm_a350_dual.json", b"min(3420.0, 60.0 * sm2_mvs", b"max(3420.0, 60.0 * sm2_mvs",
                      "SM2-GC-PURGE")

    def test_pr09_purge_parse_error_regression(self):
        self._mutated("machine/fdm_a250.json", b"local sm2_purge_ex = sm2_purge_x + (",
                      b"local sm2_purge_ex = (sm2_purge_x) + (", "SM2-GC-PURGE")

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
