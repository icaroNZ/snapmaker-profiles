#!/usr/bin/env python3
"""Tests for scripts/validate_profiles.py and the Snapmaker 2.0 (A250/A350) preset fixes.

Run:  python3 -I scripts/test_validate_profiles.py [-v]

* Rule unit tests feed small G-code / config snippets to the SM2-* rule functions.
* The bundle tests run the validator on this repo (must PASS) and on a scratch copy with one
  regression re-applied per test (must FAIL with the matching rule ID), so every fix in the
  a250t fix pass stays covered.
* A line-ending test guards the CRLF preset files against accidental LF conversion.
* Renderer tests pin the Orca PlaceholderParser semantics that render_start_gcode.py mirrors, and purge
  tests render the SM2 start G-code for concrete footprints (PR-09); park tests pin the flush/blob point
  that follows the purge start (smart park); mesh tests pin the opt-in adaptive G1029 line (PR-23).
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
        # a250t F3: single flush without the 250 floor is still found and checked
        f3 = "M104 S{min(275, min(nozzle_temperature_range_high[0], nozzle_temperature_initial_layer[0] + 15))}"
        self.assertEqual(vp.rule_gc_flush(f3, single=True), [])
        self.assertEqual(len(vp.rule_gc_flush("M104 S{min(275, nozzle_temperature_initial_layer[0] + 15)}",
                                              single=True)), 1)
        self.assertEqual(len(vp.rule_gc_flush("M104 S{nozzle_temperature_initial_layer[0] + 15}", single=True)), 2)

    def test_screentemp_rule(self):
        """a250t F1: sacrificial M104/M140 copies on layer_num 1 only, real values, no waits."""
        single = ("{if layer_num == 1}M104 S{nozzle_temperature[current_extruder]} ; sacrificial\n"
                  "M104 S{nozzle_temperature[current_extruder]}\nM140 S{bed_temperature[current_extruder]} ; s\n"
                  "M140 S{bed_temperature[current_extruder]}\n{endif}")
        self.assertEqual(vp.rule_gc_screentemp(single, dual=False), [])
        self.assertEqual([r for r, _ in vp.rule_gc_screentemp("", dual=False)], ["SM2-GC-SCREENTEMP"])
        every_layer = single.replace("{if layer_num == 1}", "{if true}")
        self.assertIn("SM2-GC-SCREENTEMP", [r for r, _ in vp.rule_gc_screentemp(every_layer, dual=False)])
        no_copy = single.replace("M104 S{nozzle_temperature[current_extruder]} ; sacrificial\n", "")
        self.assertIn("SM2-GC-SCREENTEMP", [r for r, _ in vp.rule_gc_screentemp(no_copy, dual=False)])
        wrong = single.replace("bed_temperature[", "nozzle_temperature[")
        self.assertIn("SM2-GC-SCREENTEMP", [r for r, _ in vp.rule_gc_screentemp(wrong, dual=False)])
        wait = single.replace("{endif}", "M109 S{nozzle_temperature[current_extruder]}\n{endif}")
        self.assertIn("SM2-GC-SCREENTEMP", [r for r, _ in vp.rule_gc_screentemp(wait, dual=False)])
        bed = "max(is_extruder_used[0] ? bed_temperature[0] : 0, is_extruder_used[1] ? bed_temperature[1] : 0)"
        dual = "{if layer_num == 1}M140 S{%s} ; s\nM140 S{%s}\n{endif}" % (bed, bed)
        self.assertEqual(vp.rule_gc_screentemp(dual, dual=True), [])
        cur_only = dual.replace(bed, "bed_temperature[current_extruder]")  # not Orca's max over used extruders
        self.assertIn("SM2-GC-SCREENTEMP", [r for r, _ in vp.rule_gc_screentemp(cur_only, dual=True)])

    def test_cfg_rule(self):
        """a250t F6/F8/F11."""
        good = {"host_type": "snapmaker2", "disable_m73": "1", "machine_max_junction_deviation": ["0.02"]}
        self.assertEqual(vp.rule_cfg(good), [])
        for key, val in (("host_type", "octoprint"), ("disable_m73", "0"), ("machine_max_junction_deviation", ["0"]),
                         ("machine_max_junction_deviation", ["0.5"])):
            bad = dict(good, **{key: val})
            self.assertEqual([k for k, _ in vp.rule_cfg(bad)], [key], (key, val))
        self.assertEqual(sorted(k for k, _ in vp.rule_cfg({})),
                         ["disable_m73", "host_type", "machine_max_junction_deviation"])

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

    def test_regex_match(self):
        """Orca '=~' / '!~': whole-string boost::regex_match, '.' spans newlines (PlaceholderParser.cpp:657-683)."""
        tpl = "{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}on{else}off{endif}"
        self.assertEqual(self.r(tpl, printer_notes="PRINTER_MODEL_SNAPMAKER_A250\nSM2_ADAPTIVE_MESH\n"), "on")
        self.assertEqual(self.r(tpl, printer_notes="PRINTER_MODEL_SNAPMAKER_A250\n"), "off")
        self.assertEqual(self.r(tpl, printer_notes=""), "off")
        self.assertEqual(self.r("{if s=~/A250/}y{else}n{endif}", s="A250 Dual"), "n")  # anchored: no partial match
        self.assertEqual(self.r("{if s!~/.*a\\/b.*/ and 2 > 1}y{else}n{endif}", s="x a/b"), "n")  # escaped '/'
        self.assertEqual(self.r("{8/2}"), "4")  # '/' outside a regex is still division
        with self.assertRaises(rsg.RenderError):
            self.r("{if n=~/x/}y{endif}", n=1)  # Orca: left hand side of a regex match must be a string

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
        moves = [m["line"] for m in rsg.gcode_moves(text)]
        after_lift = moves[moves.index("G0 Z5.6 F200") + 1:]  # purge moves: after the lift off the flush blob
        purge = [l for l in after_lift if l.startswith(("G0 X", "G1 X"))]
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


class ParkTests(unittest.TestCase):
    """Smart park: the flush wait and E4 blob sit beside the purge start instead of the fixed X10 Y0 corner."""

    def render(self, printer, footprint, ie=0, mvs=15.0):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
        text, local = rsg.render(cfg["machine_start_gcode"], rsg.sample_variables(cfg, footprint, ie, mvs), ie)
        moves = rsg.gcode_moves(text)
        blob = [m for m in moves if not m["xy"] and m["e"] and m["e"] > 0][0]  # G1 E4, the first stationary prime
        return text, local, moves, (blob["x0"], blob["y0"])

    def test_corner_part_no_longer_gets_the_blob(self):
        fp = (0, 0, 30, 30)
        self.assertEqual(vp._rect_gap((10, 0, 10, 0), fp), 0.0)  # the old X10 Y0 blob landed on this part
        text, local, moves, blob = self.render("Snapmaker A250 (0.4 nozzle)", fp)
        self.assertIn("; SM2_PARK start X55.00 Y0.00", text)       # purge "right" at X45, blob 10 mm further right
        self.assertEqual(blob, (55.0, 0.0))
        self.assertEqual(moves[0]["line"], "G0 X55.00 Y0.00 F3420.0")
        self.assertGreaterEqual(vp._rect_gap((55, 0, 55, 0), fp), 25.0)
        self.assertNotIn("G0 X10 F3420.0", text)

    def test_centre_part_parks_behind_and_in_front_of_the_purge_start(self):
        text, local, _, blob = self.render("Snapmaker A250 (0.4 nozzle)", (90, 100, 140, 150))
        self.assertEqual((local["sm2_purge_x"], local["sm2_purge_y"]), (90.0, 85.0))
        self.assertEqual(blob, (80.0, 75.0))
        self.assertFalse(local["sm2_park_at_end"])

    def test_clamped_start_moves_the_blob_past_the_first_pass_end(self):
        # front purge at X10 Y5: 10 mm behind/in front would clamp to 5 mm from the start, where the nozzle lands
        text, local, _, blob = self.render("Snapmaker A250 (0.4 nozzle)", (5, 20, 50, 60))
        self.assertIn("; SM2_PARK end X120.00 Y0.00", text)
        self.assertEqual(blob, (120.0, 0.0))
        _, local, _, blob = self.render("Snapmaker A250 (0.4 nozzle)", (30, 0, 80, 40))  # left purge X15 Y0..100
        self.assertEqual((local["sm2_purge_side"], blob), ("left", (10.0, 110.0)))

    def test_fallback_keeps_the_old_corner(self):
        text, local, moves, blob = self.render("Snapmaker A250 (0.4 nozzle)", (5, 5, 225, 245))
        self.assertEqual((local["sm2_purge_side"], blob), ("fallback", (10.0, 0.0)))
        _, _, _, blob = self.render("Snapmaker A350 (0.4 nozzle)", (5, 5, 315, 345))
        self.assertEqual(blob, (10.0, 0.0))

    def test_dual_t1_stays_in_reach(self):
        for fp in ((90, 100, 140, 150), (0, 0, 30, 30), (200, 0, 230, 30), (5, 20, 50, 60)):
            _, local, _, blob = self.render("Snapmaker A250 Dual (0.4 nozzle)", fp, ie=1, mvs=[15.0, 15.0])
            self.assertTrue(10.0 <= blob[0] <= 220.0 and blob[1] >= 0.0, (fp, blob))

    def test_heat_order_and_mesh_slot(self):
        """Order kept from PR-09: home, bed wait, (mesh slot), park at Z1.6, flush wait, blob, lift, purge."""
        for base in FixedTemplateTests.BASES:
            lines = template(base, "machine_start_gcode").split("\n")
            def at(prefix):
                hits = [i for i, l in enumerate(lines) if l.startswith(prefix)]
                self.assertEqual(len(hits), 1, (base, prefix, hits))
                return hits[0]
            # A350 single lowers with "G1 Z0.4" (the drift rule reads G1 without E as G0)
            order = [at("G28"), at("M190 R"), at("; SM2_MESH "), at("G0 X{digits(sm2_park_x"), at("G0 Z1.6 F960.0"),
                     at("M109 S{min(2"), at(("G0 Z0.4 F200", "G1 Z0.4 F200")), at("G1 E4 F"), at("G0 Z5.6 F200"),
                     at("G0 X{digits(sm2_purge_x, 0, 2)}"), at("G0 Z0.3 F960.0"), at("M109 S{nozzle_temperature_initial")]
            self.assertEqual(order, sorted(order), base)
            self.assertEqual(lines[at("; SM2_MESH ") - 1], "M190 R{first_layer_bed_temperature[initial_extruder]}")
            self.assertTrue(lines[at("M109 S{min(2")].endswith(" C2 W1"), base)

    def test_rule_passes_on_every_base(self):
        for printer in ("Snapmaker A250 (0.4 nozzle)", "Snapmaker A250 Dual QS+B Kit (0.2 nozzle)",
                        "Snapmaker A350 QSKit (0.8 nozzle)", "Snapmaker A350 Dual (0.4 nozzle)"):
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
            self.assertEqual(vp.rule_gc_park(cfg["machine_start_gcode"], cfg), [], printer)

    def test_rule_catches_fixed_corner(self):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), "Snapmaker A250 (0.4 nozzle)")
        start = cfg["machine_start_gcode"]
        fixed = start.replace("G0 X{digits(sm2_park_x, 0, 2)} Y{digits(sm2_park_y, 0, 2)} F3420.0",
                              "G0 X10 F3420.0\nG0 Y0 F3420.0")
        self.assertNotEqual(fixed, start)
        found = vp.rule_gc_park(fixed, cfg)
        self.assertEqual([r for r, _ in found], ["SM2-GC-PARK"])
        self.assertIn("SM2_PARK point", found[0][1])


class MeshTests(unittest.TestCase):
    """PR-23: opt-in adaptive G1029 region probe after M190 R (controller SM2-5.2.1-a250t.8 and later)."""

    def render(self, printer, footprint, adaptive, ie=0, margin=None, notes=None):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
        var = rsg.sample_variables(cfg, footprint, ie, adaptive=adaptive, margin=margin, printer_notes=notes)
        return rsg.render(cfg["machine_start_gcode"], var, ie)[0]

    @staticmethod
    def g1029(text):
        return [l for l in text.split("\n") if l.startswith("G1029")]

    def test_off_by_default_and_output_unchanged(self):
        """Without the token the start G-code is byte-identical to the template with the {if} block removed."""
        import re
        for base in ("fdm_a250.json", "fdm_a350.json"):
            start = template(base, "machine_start_gcode")
            block = re.findall(r"\{if printer_notes=~/\.\*SM2_ADAPTIVE_MESH\.\*/\}.*?\{endif\}", start, re.S)
            self.assertEqual(len(block), 1, base)
            stripped = start.replace(block[0], "")
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), "Snapmaker %s (0.4 nozzle)" % base[4:8].upper())
            for _, fp, _ in vp.purge_footprints(rsg.usable_bed(cfg)):
                var = rsg.sample_variables(cfg, fp)
                self.assertNotIn(rsg.ADAPTIVE_MESH_TOKEN, var["printer_notes"])
                off = rsg.render(start, var)[0]
                self.assertEqual(off, rsg.render(stripped, var)[0], (base, fp))
                self.assertEqual(self.g1029(off), [], (base, fp))

    def test_exact_lines(self):
        cases = [  # (printer, footprint, margin, expected line)
            ("Snapmaker A250 (0.4 nozzle)", (90, 100, 140, 150), None, "G1029 A L90.00 R140.00 F100.00 B150.00"),
            ("Snapmaker A250 (0.4 nozzle)", (0, 0, 30, 30), 5, "G1029 A L0.00 R35.00 F0.00 B35.00"),  # bed clamp
            ("Snapmaker A250 QSKit (0.8 nozzle)", (195.5, 5.25, 225.75, 35), 2, "G1029 A L193.50 R227.75 F3.25 B37.00"),
            ("Snapmaker A350 (0.4 nozzle)", (300, 320, 320, 350), 10, "G1029 A L290.00 R320.00 F310.00 B350.00"),
        ]
        for printer, fp, margin, want in cases:
            self.assertEqual(self.g1029(self.render(printer, fp, True, margin=margin)), [want], (printer, fp))

    def test_position_after_bed_wait_before_park(self):
        for printer in ("Snapmaker A250 (0.4 nozzle)", "Snapmaker A350 BKit (0.6 nozzle)"):
            lines = self.render(printer, (90, 100, 140, 150), True).split("\n")
            k = next(i for i, l in enumerate(lines) if l.startswith("G1029 A "))
            self.assertTrue(lines[k - 2].startswith("; SM2_MESH saved bed mesh"), lines[k - 2])
            self.assertTrue(lines[k - 1].startswith("; SM2_MESH_ADAPTIVE on"), lines[k - 1])
            self.assertTrue(lines[k - 3].startswith("M190 R"), lines[k - 3])
            first_move = next(i for i, l in enumerate(lines) if l.startswith(("G0 ", "G1 ")))
            self.assertLess(k, first_move)
            self.assertTrue(lines[first_move].startswith("G0 X"), lines[first_move])  # park move unchanged

    def test_opt_in_only_adds_the_mesh_lines(self):
        for printer in ("Snapmaker A250 (0.4 nozzle)", "Snapmaker A350 QS+B Kit (0.4 nozzle)"):
            for fp in ((0, 0, 30, 30), (90, 100, 140, 150), (5, 5, 225, 205)):
                off = self.render(printer, fp, False).split("\n")
                on = [l for l in self.render(printer, fp, True).split("\n")
                      if not l.startswith(("G1029 A ", "; SM2_MESH_ADAPTIVE "))]
                self.assertEqual(on, off, (printer, fp))

    def test_token_anywhere_in_multiline_notes(self):
        notes = "PRINTER_MODEL_SNAPMAKER_A250\nPRINTER_MODEL_SNAPMAKER_A250_QUICKSWAPKIT\nmy notes SM2_ADAPTIVE_MESH here\n"
        self.assertEqual(len(self.g1029(self.render("Snapmaker A250 (0.4 nozzle)", (90, 100, 140, 150), False,
                                                    notes=notes))), 1)
        self.assertEqual(self.g1029(self.render("Snapmaker A250 (0.4 nozzle)", (90, 100, 140, 150), False,
                                                notes="sm2_adaptive_mesh\n")), [])  # case-sensitive, as Orca

    def test_dual_never_probes(self):
        """The controller refuses G1029 A for the dual extruder (G1029.cpp:337-340), so the Dual bases have no line."""
        for base in ("fdm_a250_dual.json", "fdm_a350_dual.json"):
            code = vp.gcode_code_lines(template(base, "machine_start_gcode"))
            self.assertEqual([c for c in code if "G1029" in c or "SM2_ADAPTIVE_MESH" in c], [], base)
        for ie in (0, 1):
            text = self.render("Snapmaker A250 Dual (0.4 nozzle)", (90, 100, 140, 150), True, ie=ie)
            self.assertEqual(self.g1029(text), [])

    def test_rule_passes_on_every_base(self):
        for printer in ("Snapmaker A250 (0.4 nozzle)", "Snapmaker A250 QS+B Kit (0.2 nozzle)",
                        "Snapmaker A350 QSKit (0.8 nozzle)", "Snapmaker A350 Dual (0.4 nozzle)",
                        "Snapmaker A250 Dual QS+B Kit (0.4 nozzle)"):
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
            self.assertEqual(vp.rule_gc_mesh(cfg["machine_start_gcode"], cfg, " Dual" in printer), [], printer)

    def test_rule_catches_unconditional_and_dual(self):
        cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), "Snapmaker A250 (0.4 nozzle)")
        start = cfg["machine_start_gcode"]
        always = start.replace("{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}", "{if true}")
        self.assertNotEqual(always, start)
        found = vp.rule_gc_mesh(always, cfg, False)
        self.assertEqual([r for r, _ in found], ["SM2-GC-MESH"])
        self.assertIn("opt-in", found[0][1])
        dcfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), "Snapmaker A250 Dual (0.4 nozzle)")
        self.assertEqual([r for r, _ in vp.rule_gc_mesh(start, dcfg, True)], ["SM2-GC-MESH"])  # single block on Dual


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
            pause = template(base, "machine_pause_gcode")
            self.assertEqual(pause, "M600", base)  # a250t F10 is fixed in the controller (a250t.12), not here
            self.assertEqual(vp.rule_gc_pause(pause), [], base)

    def test_a250t_f1_f3_f6_f8_f11(self):
        presets = rsg.load_machine_presets(ROOT)
        for base in self.BASES:
            dual = "dual" in base
            start = template(base, "machine_start_gcode")
            # F3: single head flushes at first layer + 15 (no 250 floor); Dual keeps Snapmaker's floor
            self.assertEqual("max(250" in start, dual, base)
            self.assertEqual(vp.rule_gc_screentemp(template(base, "layer_change_gcode"), dual), [], base)
            cfg = rsg.resolve_machine(presets, base[:-5])
            self.assertEqual(vp.rule_cfg(cfg), [], base)
            self.assertEqual(cfg["host_type"], "snapmaker2")
            self.assertEqual(cfg["machine_max_junction_deviation"], ["0.02"])
        # every A250/A350 printer (kits, nozzles) inherits them
        names = [n for n in presets if vp.SM2_PRINTER_RE.match(n)]
        self.assertGreater(len(names), 60)
        for name in names:
            cfg = rsg.resolve_machine(presets, name)
            self.assertEqual(vp.rule_cfg(cfg), [], name)
            self.assertIn("layer_change_gcode", cfg, name)

    def test_f3_flush_render(self):
        """a250t F3: PLA (first layer 210, range_high 240) flushes at 225 on single, 240 on Dual (250 floor, range_high cap)."""
        for printer, want in (("Snapmaker A250 (0.4 nozzle)", 225), ("Snapmaker A350 (0.4 nozzle)", 225),
                              ("Snapmaker A250 Dual (0.4 nozzle)", 240)):
            cfg = rsg.resolve_machine(rsg.load_machine_presets(ROOT), printer)
            text, _ = rsg.render(cfg["machine_start_gcode"], rsg.sample_variables(cfg, (90, 100, 140, 150)))
            flush = [ln for ln in text.splitlines() if ln.startswith("M109") and "C2 W1" in ln]
            self.assertEqual(flush, ["M109 S%d C2 W1" % want], printer)

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

    def test_park_fixed_corner_regression(self):
        self._mutated("machine/fdm_a250.json", b"G0 X{digits(sm2_park_x, 0, 2)} Y{digits(sm2_park_y, 0, 2)} F3420.0",
                      b"G0 X10 F3420.0\\nG0 Y0 F3420.0", "SM2-GC-PARK")

    def test_park_sign_regression(self):  # blob towards the part instead of away from it
        self._mutated("machine/fdm_a350.json", b"sm2_purge_sx > 0 ? 0.0 - sm2_park_d : sm2_purge_sx < 0 ? sm2_park_d",
                      b"sm2_purge_sx > 0 ? sm2_park_d : sm2_purge_sx < 0 ? 0.0 - sm2_park_d", "SM2-GC-PARK")

    def test_park_clamp_regression(self):
        self._mutated("machine/fdm_a250_dual.json", b"local sm2_park_y1 = max(sm2_by0, sm2_purge_y + sm2_park_oy",
                      b"local sm2_park_y1 = min(sm2_by0, sm2_purge_y + sm2_park_oy", "SM2-GC-PARK")

    def test_park_no_end_fallback_regression(self):  # clamped blob 5 mm from where the nozzle lands for the purge
        self._mutated("machine/fdm_a350_dual.json", b"sm2_park_d * sm2_park_d - 0.01}", b"0.0 - 1.0}", "SM2-GC-PARK")

    def test_pr23_mesh_always_on_regression(self):  # stock firmware would probe the full grid on every print
        self._mutated("machine/fdm_a250.json", b"{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}", b"{if true}",
                      "SM2-GC-MESH")

    def test_pr23_mesh_heat_soak_regression(self):  # H would override the screen's bed temperature for the job
        self._mutated("machine/fdm_a350.json", b"adaptive_bed_mesh_max[1])), 0, 2)}\\n{endif}",
                      b"adaptive_bed_mesh_max[1])), 0, 2)} H{first_layer_bed_temperature[initial_extruder]}\\n{endif}",
                      "SM2-GC-MESH")

    def test_pr23_mesh_unclamped_regression(self):
        self._mutated("machine/fdm_a250.json",
                      b"L{digits(max(print_bed_min[0], min(print_bed_max[0], adaptive_bed_mesh_min[0])), 0, 2)}",
                      b"L{digits(adaptive_bed_mesh_min[0], 0, 2)}", "SM2-GC-MESH")

    def test_pr23_mesh_swapped_bound_regression(self):
        self._mutated("machine/fdm_a350.json", b"min(print_bed_max[1], adaptive_bed_mesh_min[1])), 0, 2)} B",
                      b"min(print_bed_max[1], adaptive_bed_mesh_max[1])), 0, 2)} B", "SM2-GC-MESH")

    def test_pr23_mesh_before_bed_wait_regression(self):
        self._mutated("machine/fdm_a250.json", b"M140 S{first_layer_bed_temperature[initial_extruder]}\\n",
                      b"M140 S{first_layer_bed_temperature[initial_extruder]}\\n{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}"
                      b"G1029 A L0.00 R10.00 F0.00 B10.00\\n{endif}", "SM2-GC-MESH")

    def test_pr23_mesh_on_dual_regression(self):
        self._mutated("machine/fdm_a250_dual.json", b"M190 R{first_layer_bed_temperature[initial_extruder]}\\n",
                      b"M190 R{first_layer_bed_temperature[initial_extruder]}\\n{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}"
                      b"G1029 A L0.00 R10.00 F0.00 B10.00\\n{endif}", "SM2-GC-MESH")

    def test_pr01_pause_empty_regression(self):
        self._mutated("machine/fdm_a250.json", b'"machine_pause_gcode": "M600"', b'"machine_pause_gcode": ""',
                      "SM2-GC-PAUSE")

    def test_pr01_pause_missing_regression(self):
        # Dual base without the key: it then resolves to nothing (fdm_linear2_dual/fdm_linear2/fdm_common lack it)
        path = os.path.join(MACHINE, "fdm_a250_dual.json")
        with open(path, "rb") as fh:
            line = next(ln for ln in fh.read().split(b"\r\n") if ln.startswith(b'  "machine_pause_gcode": '))
        self._mutated("machine/fdm_a250_dual.json", line + b"\r\n", b"", "SM2-GC-PAUSE")

    def test_f1_every_layer_regression(self):  # would undo live temperature changes from the screen
        self._mutated("machine/fdm_a250.json", b'"layer_change_gcode": "{if layer_num == 1}',
                      b'"layer_change_gcode": "{if layer_num >= 1}', "SM2-GC-SCREENTEMP")

    def test_f1_missing_regression(self):
        self._mutated("machine/fdm_a350_dual.json", b'"layer_change_gcode": "{if layer_num == 1}',
                      b'"layer_change_gcode": "{if layer_num == 99}', "SM2-GC-SCREENTEMP")

    def test_f1_drift_regression(self):
        self._mutated("machine/fdm_a350.json", b"M104 S{nozzle_temperature[current_extruder]}\\n{endif}",
                      b"M104 S{nozzle_temperature[current_extruder] + 5}\\n{endif}", "SM2-GC-DRIFT")

    def test_f3_flush_uncapped_regression(self):
        self._mutated("machine/fdm_a250.json",
                      b"M104 S{min(275, min(nozzle_temperature_range_high[(initial_extruder % 2 == 0 ? min(initial_extruder"
                      b" + initial_extruder, 63) : max(initial_extruder - (1-initial_extruder), 0))], ",
                      b"M104 S{min(275, min(999, ", "SM2-GC-FLUSH")

    def test_f6_host_type_regression(self):
        self._mutated("machine/fdm_a350.json", b'"host_type": "snapmaker2"', b'"host_type": "octoprint"', "SM2-CFG")

    def test_f8_junction_deviation_regression(self):
        self._mutated("machine/fdm_a250_dual.json", b'"machine_max_junction_deviation": [\r\n    "0.02"',
                      b'"machine_max_junction_deviation": [\r\n    "0"', "SM2-CFG")

    def test_f11_m73_regression(self):
        self._mutated("machine/fdm_a250.json", b'"disable_m73": "1"', b'"disable_m73": "0"', "SM2-CFG")

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
