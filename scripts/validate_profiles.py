#!/usr/bin/env python3
"""Static validator for the Snapmaker Orca vendor preset bundle.

It mirrors the checks Snapmaker Orca / OrcaSlicer performs (or silently relies
on) when it loads ``Snapmaker.json`` in ``PresetBundle::load_vendor_configs_from_json``:

* every file listed in ``Snapmaker.json`` exists, parses as JSON and its
  ``name``/``type`` match the index entry; names are unique per list;
* ``inherits`` resolves to a preset listed EARLIER in the same list (Orca builds
  its parent map while it walks each list in order; a later or missing parent
  aborts loading of the whole vendor), or, for filaments, to a preset of the
  OrcaFilamentLibrary base bundle;
* ``instantiation`` is present and is "true"/"false";
* ``compatible_printers`` only names instantiated machine presets;
* machine presets: ``printer_model`` is a listed machine_model,
  ``printer_variant`` is one of the model's nozzle sizes, default print /
  filament profiles exist, nozzle diameter > 0, printable area / height within
  the published build volume of the Snapmaker 2.0 model;
* filaments (after resolving the full ``inherits`` chain): density and
  diameter present and > 0;
* JSON files on disk under Snapmaker/{machine,filament,process} that are not
  referenced from ``Snapmaker.json`` (orphans);
* Snapmaker 2.0 content rules (rule ID first in every log line, counts per rule
  in the summary), checked against the SM2 controller firmware:
  SM2-LIM-Z     emitted Z accel <= firmware 100 mm/s^2 (M201 does not clamp it)
  SM2-LIM-XY    XY max speed <= firmware 120 mm/s (WARNING)
  SM2-TEMP-MAX  filament nozzle temps <= 275 (single 3DP head) / 300 (Dual)
  SM2-RETRACT-E filament_retraction_speed <= machine_max_speed_e (WARNING)
  SM2-DEF-COMPAT default filament/print profiles are compatible with the printer
  SM2-COMPAT-KIT a preset covers every kit variant of a family it names (WARNING)
  SM2-GC-MCODE  every M-code in the G-code templates is handled by the controller
  SM2-GC-DRIFT  A250 and A350 twin templates only differ in bed coordinates
  SM2-GC-BEDTYPE curr_bed_type comparisons use a stored BedType value
  SM2-GC-HOTBED no Z move below 1 mm between G28 and the first XY move
  SM2-GC-FLUSH  start flush temperature is capped at nozzle_temperature_range_high
                (and 275 on single-head printers)
  SM2-GC-BEDMIN change_filament_gcode never lowers the bed with min() (Orca #552)
  SM2-GC-PAUSE  machine_pause_gcode resolves to a template with an M600 line (Orca's
                "Pause" item writes only this key after ;PAUSE_PRINT; an empty value
                means the print never stops; M600 is the controller's runout pause)
  SM2-GC-PURGE  start G-code purge line uses Orca's first-layer footprint, nozzle and filament MVS
                placeholders, and (rendered offline by render_start_gcode.py for sample footprints) every
                G0/G1 stays on the bed and outside bed_exclude_area, the purge line keeps clear of the
                footprint unless it falls back to the old X40 Y0 line, and its flow stays <= filament MVS

Findings that involve the Snapmaker 2.0 A250/A350 presets (directly, or as a
base of them) are ERRORs and make the script exit with status 1. Findings on
other printers (U1, J1, Artisan) are reported as OTHER and do not fail the
run unless ``--strict`` is given.

Usage:  python3 scripts/validate_profiles.py [--root DIR] [--orca-filament-library DIR] [--strict] [-v]
"""

import argparse
import glob
import importlib.util
import json
import logging
import os
import re
import sys

log = logging.getLogger("validate_profiles")

VENDOR = "Snapmaker"
LISTS = (  # index key -> expected preset "type"
    ("machine_model_list", "machine_model"),
    ("process_list", "process"),
    ("filament_list", "filament"),
    ("machine_list", "machine"),
)
# Rule files deployed by fixed path (cmake/SyncSnapmakerProfiles.cmake, PresetUpdater), not via the index.
RULE_FILES = {
    "filament/filament_allow_list.json",
    "filament/filament_compatibility.json",
    "filament/filament_hot_bed_nozzles.json",
    "filament/filaments_colours.json",
}
SM2_RE = re.compile(r"\bA(250|350)\b")
# Published Snapmaker 2.0 3D-printing build volume (X, Y, Z) in mm.
SM2_SPEC = {"A250": (230.0, 250.0, 235.0), "A350": (320.0, 350.0, 330.0)}

# ---- Snapmaker 2.0 firmware facts (Snapmaker2-Controller a250t/main) used by the SM2-* rules ----
FW_MAX_ACCEL_Z = 100.0       # Marlin/Configuration.h:718 DEFAULT_MAX_ACCELERATION {3000, 3000, 100, ...}
FW_MAX_FEEDRATE_XY = 120.0   # Marlin/Configuration.h:705 DEFAULT_MAX_FEEDRATE {120, 120, 40, 45, 40}
HOTEND_MAX_SINGLE = 275.0    # snapmaker/src/module/toolhead_3dp.cpp:102 UpdateHotendMaxTemp(275)
HOTEND_MAX_DUAL = 300.0      # snapmaker/src/module/toolhead_dualextruder.cpp:194-195 UpdateHotendMaxTemp(300, e)
# Every M-code with a `case` in GcodeSuite::process_parsed_command (Marlin/src/gcode/gcode.cpp:421 ff.), including
# ones behind #if guards; an M-code outside this set is silently ignored ("Unknown command") by the controller.
SM2_MCODES = frozenset(int(c) for c in """
0 1 3 4 5 7 8 9 12 17 18 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 42 43 48 49 73 75 76 77 78 80 81 82 83 84 85 92
100 101 104 105 106 107 108 109 110 111 112 113 114 115 117 118 119 120 121 122 125 126 127 128 129 140 141 145 149 150
155 163 164 165 166 190 191 200 201 202 203 204 205 206 207 208 209 211 217 218 220 221 226 240 250 260 261 280 281 290
300 301 302 303 304 350 351 355 360 361 362 363 364 380 381 400 401 402 403 404 405 406 407 410 412 413 420 421 422 425
428 493 500 501 502 503 504 524 540 569 600 603 605 665 666 701 702 800 810 811 812 813 814 815 816 817 818 819 851 852
860 861 862 863 864 865 866 867 868 869 876 900 906 907 908 909 910 911 912 913 914 916 917 918 928 951 997 999 1000
1005 1006 1007 1010 1011 1012 1026 1028 1029 1999 2000 2001 2002 3000 3001 3002 3003 3004 3005 7219""".split())
# Stored BedType values (OrcaSlicer src/libslic3r/PrintConfig.cpp s_keys_map_BedType). The UI shows other labels,
# e.g. "Smooth PEI Plate / High Temp Plate" is stored as "High Temp Plate" (upstream PR2 lesson).
ORCA_BED_TYPES = frozenset({"Default Plate", "Supertack Plate", "Cool Plate", "Engineering Plate", "High Temp Plate",
                            "Textured PEI Plate", "Textured Cool Plate", "Graphic Effect Plate"})
GCODE_KEYS = ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode", "before_layer_change_gcode",
              "layer_change_gcode", "machine_pause_gcode", "pause_gcode", "template_custom_gcode",
              "time_lapse_gcode", "wrapping_detection_gcode")
SM2_TWINS = (("fdm_a250", "fdm_a350"), ("fdm_a250_dual", "fdm_a350_dual"))
DRIFT_KEYS = ("machine_start_gcode", "machine_end_gcode", "change_filament_gcode", "before_layer_change_gcode",
              "machine_pause_gcode")
# SM2-GC-PURGE (PR-09): placeholders the purge must use (Orca GCode.cpp:3040-3042 first_layer_print_*, PrintConfig
# nozzle_diameter / filament_max_volumetric_speed coFloats), the comment it writes, and its limits.
PURGE_PLACEHOLDERS = ("first_layer_print_min", "first_layer_print_max", "nozzle_diameter[",
                      "filament_max_volumetric_speed[", "print_bed_min", "print_bed_max")
PURGE_MARKER_RE = re.compile(r"^; SM2_PURGE (front|left|right|fallback) start X(-?[\d.]+) Y(-?[\d.]+)", re.M)
PURGE_MAX_F = 3420.0          # mm/min, the old fixed purge feed
PURGE_CLEARANCE = 4.0         # mm between purge extrusion and first-layer footprint (template: 5 mm)
SM2_PRINTER_RE = re.compile(r"^Snapmaker (A250|A350)( Dual)?( QSKit| BKit| QS\+B Kit)? \((\d+(?:\.\d+)?) nozzle\)$")


class Report:
    def __init__(self):
        self.errors, self.other, self.warnings, self.infos = [], [], [], []
        self.rule_counts = {}  # rule id -> number of findings (errors, other and warnings)

    def _count(self, rule, msg):
        if rule:
            self.rule_counts[rule] = self.rule_counts.get(rule, 0) + 1
            return "%s %s" % (rule, msg)
        return msg

    def issue(self, sm2, msg, rule=None):
        msg = self._count(rule, msg)
        if sm2:
            self.errors.append(msg)
            log.error("[A250/A350] %s", msg)
        else:
            self.other.append(msg)
            log.warning("[other] %s", msg)

    def warn(self, msg, rule=None):
        msg = self._count(rule, msg)
        self.warnings.append(msg)
        log.warning("[warn] %s", msg)

    def info(self, msg):
        self.infos.append(msg)
        log.info("[info] %s", msg)


def load_json(path, report):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        # nlohmann::json (used by Orca) skips a leading UTF-8 BOM, so this loads; flag it anyway.
        report.warn("%s starts with a UTF-8 BOM (Orca tolerates it, strict JSON tools do not)" % path)
    return json.loads(raw.decode("utf-8-sig"))


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def to_float(value):
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def is_sm2_name(name):
    return bool(SM2_RE.search(name or ""))


def sm2_model_of(name):
    m = SM2_RE.search(name or "")
    return "A" + m.group(1) if m else None


def first_float(value):
    vals = [to_float(v) for v in as_list(value)]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def gcode_code_lines(text):
    """Template lines with ';' comments removed and whitespace stripped (blank results dropped)."""
    out = []
    for line in str(text or "").split("\n"):
        code = line.split(";", 1)[0].strip()
        if code:
            out.append(code)
    return out


# ---- SM2 rule functions: pure, each returns a list of (rule_id, message) -------------------------------------------

def rule_limits(cfg):
    """SM2-LIM-Z (error) / SM2-LIM-XY (warning, returned with a 'warn:' prefix on the rule id)."""
    out = []
    if str(cfg.get("emit_machine_limits_to_gcode", "1")).strip() in ("0", "false"):
        return out
    z = first_float(cfg.get("machine_max_acceleration_z"))
    if z is not None and z > FW_MAX_ACCEL_Z:
        out.append(("SM2-LIM-Z", "machine_max_acceleration_z=%g > fw %g (Orca emits M201, which does not clamp Z"
                                 " and can be saved to EEPROM)" % (z, FW_MAX_ACCEL_Z)))
    for axis in ("x", "y"):
        v = first_float(cfg.get("machine_max_speed_" + axis))
        if v is not None and v > FW_MAX_FEEDRATE_XY:
            out.append(("warn:SM2-LIM-XY", "machine_max_speed_%s=%g > fw %g (unverified for T modules)"
                        % (axis, v, FW_MAX_FEEDRATE_XY)))
    return out


def rule_gc_mcode(text):
    out = []
    for code in gcode_code_lines(text):
        m = re.match(r"M(\d+)\b", code)
        if m and int(m.group(1)) not in SM2_MCODES:
            out.append(("SM2-GC-MCODE", "M%s is not handled by the SM2 controller (gcode.cpp): %r" % (m.group(1), code)))
    return out


def rule_gc_bedtype(text):
    out = []
    for m in re.finditer(r'curr_bed_type\s*[!=]=\s*\\?"([^"\\]+)\\?"', str(text or "")):
        if m.group(1) not in ORCA_BED_TYPES:
            out.append(("SM2-GC-BEDTYPE", "curr_bed_type compared with %r, which is a UI label, not a stored value %s"
                        % (m.group(1), sorted(ORCA_BED_TYPES))))
    return out


def rule_gc_hotbed(start):
    """No Z move below 1 mm between G28 and the first XY move (nozzle dragged across the bed at the home corner)."""
    lines = gcode_code_lines(start)
    try:
        i = next(k for k, c in enumerate(lines) if re.match(r"G28\b", c))
    except StopIteration:
        return []
    for code in lines[i + 1:]:
        m = re.match(r"G[01]\b(.*)", code)
        if not m:
            continue
        if re.search(r"[XY]-?[\d.{]", m.group(1)):
            break
        z = re.search(r"Z(-?\d+(?:\.\d+)?)", m.group(1))
        if z and float(z.group(1)) < 1.0:
            return [("SM2-GC-HOTBED", "%r after G28 before the first XY move (nozzle travels at Z%s)"
                     % (code, z.group(1)))]
    return []


def rule_gc_flush(start, single):
    out = []
    for code in gcode_code_lines(start):
        if re.match(r"M10[49]\b", code) and "max(250" in code.replace(" ", ""):
            if "nozzle_temperature_range_high" not in code:
                out.append(("SM2-GC-FLUSH", "flush %r ignores nozzle_temperature_range_high" % code[:60]))
            if single and "min(%d" % HOTEND_MAX_SINGLE not in code.replace(" ", ""):
                out.append(("SM2-GC-FLUSH", "flush %r is not capped at the single head max %g"
                            % (code[:60], HOTEND_MAX_SINGLE)))
    return out


def rule_gc_bedmin(change):
    if re.search(r"M140\s+S\{\s*min\(", str(change or "")):
        return [("SM2-GC-BEDMIN", "change_filament_gcode lowers the bed with M140 S{min(...)}: Orca already set the"
                                  " max over all used filaments, nothing raises it again (Orca #552)")]
    return []


def rule_gc_pause(text):
    """SM2-GC-PAUSE: Orca's "Pause" item writes only machine_pause_gcode after ;PAUSE_PRINT (Orca GCode.cpp:5011-5018;
    the legacy key pause_gcode is unknown to Orca and dropped on load). On the SM2 controller (no
    ADVANCED_PAUSE_FEATURE) M600 arms the filament-runout pause: Snapmaker2-Controller
    Marlin/src/gcode/feature/pause/M600.cpp:163-174."""
    lines = gcode_code_lines(text)
    if not lines:
        return [("SM2-GC-PAUSE", "is missing or empty (comments only): Orca's Pause at layer"
                                 " writes only a ;PAUSE_PRINT comment and the print does not stop; use M600")]
    if not any(re.match(r"M600\b", code) for code in lines):
        return [("SM2-GC-PAUSE", "%r has no M600 line; M600 is the G-code that triggers the SM2's"
                                 " screen pause/runout flow (M600.cpp:163-174)" % "\n".join(lines)[:60])]
    return []


def _load_renderer():
    """scripts/render_start_gcode.py, loaded by path (python3 -I does not put the script dir on sys.path)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_start_gcode.py")
    spec = importlib.util.spec_from_file_location("render_start_gcode", path)
    mod = importlib.util.module_from_spec(spec)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no scripts/__pycache__ in the repo
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod


_RENDERER = []
_PURGE_CACHE = {}


def renderer():
    if not _RENDERER:
        _RENDERER.append(_load_renderer())
    return _RENDERER[0]


def purge_footprints(usable):
    """Sample first-layer footprints (x0, y0, x1, y1) on the usable bed; the bool says a side must have room."""
    x0, y0, x1, y1 = usable
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [
        ("front-left corner", (x0, y0, x0 + 30, y0 + 30), True),
        ("front-right corner", (x1 - 30, y0, x1, y0 + 30), True),
        ("back-left corner", (x0, y1 - 30, x0 + 30, y1), True),
        ("back-right corner", (x1 - 30, y1 - 30, x1, y1), True),
        ("centre", (cx - 25, cy - 25, cx + 25, cy + 25), True),
        ("full-depth strip at the left edge", (x0, y0, x0 + 20, y1), True),
        ("full-depth strip in the middle", (cx - 15, y0, cx + 15, y1), True),
        ("full-width strip at the front", (x0, y0, x1, y0 + 20), False),
        ("near-full bed", (x0 + 5, y0 + 5, x1 - 5, y1 - 5), False),
    ]


def _rect_gap(seg, rect):
    """Distance between an axis-aligned segment's bbox and a rectangle (0 when they overlap)."""
    sx0, sx1 = sorted((seg[0], seg[2]))
    sy0, sy1 = sorted((seg[1], seg[3]))
    dx = max(rect[0] - sx1, sx0 - rect[2], 0.0)
    dy = max(rect[1] - sy1, sy0 - rect[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def check_purge_render(start, cfg, footprint, initial_extruder, mvs, must_have_room):
    """Render the start G-code for one footprint; return a list of problem strings (empty = fine)."""
    rsg = renderer()
    bed, excl = rsg.bed_geometry(cfg)
    try:
        text, local = rsg.render(start, rsg.sample_variables(cfg, footprint, initial_extruder, mvs), initial_extruder)
        moves = rsg.gcode_moves(text)
    except rsg.RenderError as exc:
        return ["does not render: %s" % exc]
    m = PURGE_MARKER_RE.search(text)
    if not m:
        return ["rendered G-code has no '; SM2_PURGE <side> start X.. Y..' line"]
    side = m.group(1)
    problems = []
    if must_have_room and side == "fallback":
        problems.append("falls back to the X40 Y0 line although the footprint leaves room beside it")
    fil_d = 1.75
    fil_area = 3.14159265 * fil_d * fil_d / 4.0
    for mv in moves:
        if not mv["xy"]:
            continue
        x, y = mv["x"], mv["y"]
        if x is None or y is None:
            continue  # first move after G28 sets only one axis; the other is the (unknown) home position
        if not (bed[0] - 1e-6 <= x <= bed[2] + 1e-6 and bed[1] - 1e-6 <= y <= bed[3] + 1e-6):
            problems.append("%r leaves the bed %s" % (mv["line"], bed))
        for ex in excl:
            if ex[0] < x < ex[2] and ex[1] < y < ex[3]:
                problems.append("%r is inside bed_exclude_area %s" % (mv["line"], ex))
        if not mv["e"] or mv["e"] <= 0 or mv["x0"] is None or mv["y0"] is None:
            continue
        seg = (mv["x0"], mv["y0"], x, y)
        length = ((x - mv["x0"]) ** 2 + (y - mv["y0"]) ** 2) ** 0.5
        if side != "fallback" and _rect_gap(seg, footprint) < PURGE_CLEARANCE:
            problems.append("%r comes within %g mm of the footprint %s" % (mv["line"], PURGE_CLEARANCE, footprint))
        f = mv["f"]
        if f is not None and length > 0:
            if f > PURGE_MAX_F + 1e-6:
                problems.append("%r feed above %g" % (mv["line"], PURGE_MAX_F))
            flow = mv["e"] / length * fil_area * f / 60.0
            limit = mvs[initial_extruder] if isinstance(mvs, list) else mvs
            if limit > 0 and flow > limit * 1.01:
                problems.append("%r flows %.2f mm3/s > filament MVS %g" % (mv["line"], flow, limit))
    return problems


def rule_gc_purge(start, cfg):
    """SM2-GC-PURGE (PR-09) for one resolved SM2 printer preset."""
    text = str(start or "")
    missing = [p for p in PURGE_PLACEHOLDERS if p not in text]
    if missing:
        return [("SM2-GC-PURGE", "purge does not use Orca placeholder(s) %s (footprint/nozzle/MVS-aware purge,"
                                 " PR-09)" % ", ".join(missing))]
    rsg = renderer()
    nozzles = as_list(cfg.get("nozzle_diameter"))
    key = (text, tuple(nozzles), tuple(as_list(cfg.get("printable_area"))), tuple(as_list(cfg.get("bed_exclude_area"))))
    if key in _PURGE_CACHE:
        return _PURGE_CACHE[key]
    nozzle = to_float(nozzles[0]) if nozzles else 0.4
    mvs = 2.0 if nozzle is not None and nozzle <= 0.25 else 15.0
    out = []
    usable = rsg.usable_bed(cfg)
    extruders = (0, 1) if len(nozzles) > 1 else (0,)
    for label, fp, room in purge_footprints(usable):
        for ie in extruders:
            for sample_mvs in ((mvs, 0.0) if label == "centre" else (mvs,)):
                probs = check_purge_render(text, cfg, fp, ie, sample_mvs, room)
                if probs:
                    out.append(("SM2-GC-PURGE", "%s (T%d, MVS %g): %s" % (label, ie, sample_mvs, probs[0])))
                    break
            if out:
                break
        if out:
            break
    _PURGE_CACHE[key] = out
    return out


def drift_normalise(text):
    """Template lines with comments, blank lines and bed-size coordinates removed, G1 without E as G0."""
    out = []
    for code in gcode_code_lines(text):
        code = re.sub(r"A[23]50", "A_50", code)
        code = re.sub(r"\b([XY])-?\d+(\.\d+)?", r"\1#", code)
        if re.match(r"G1\b", code) and not re.search(r"\bE", code):
            code = "G0" + code[2:]
        out.append(code)
    return out


def rule_gc_drift(name_a, text_a, name_b, text_b, key):
    a, b = drift_normalise(text_a), drift_normalise(text_b)
    if a == b:
        return []
    import difflib
    diff = [d for d in difflib.unified_diff(a, b, lineterm="", n=0) if d[:1] in "+-" and d[:3] not in ("+++", "---")]
    return [("SM2-GC-DRIFT", "%s %s differs from %s: %s" % (name_a, key, name_b, " | ".join(diff[:6])))]


def rule_temp_max(cfg, printers):
    """printers: compatible SM2 printer names. Single head clamps to 275, Dual to 300."""
    if not printers:
        return []
    limit = HOTEND_MAX_SINGLE if any(" Dual" not in p for p in printers) else HOTEND_MAX_DUAL
    out = []
    head = "single 3DP" if limit == HOTEND_MAX_SINGLE else "Dual"
    for key in ("nozzle_temperature", "nozzle_temperature_initial_layer", "nozzle_temperature_range_high"):
        v = first_float(cfg.get(key))
        if v is not None and v > limit:
            # A requested temperature is an error; the range_high UI bound is only a warning (nothing in the SM2
            # templates prints at it once the start flush is capped, see SM2-GC-FLUSH).
            rule = "warn:SM2-TEMP-MAX" if key == "nozzle_temperature_range_high" else "SM2-TEMP-MAX"
            out.append((rule, "%s=%g > %g (%s head clamps it silently)" % (key, v, limit, head)))
    return out


def printer_family(name):
    """'Snapmaker A250 QSKit (0.4 nozzle)' -> (('A250', dual, '0.4'), 'QSKit'); None for other printers."""
    m = SM2_PRINTER_RE.match(name or "")
    if not m:
        return None
    return (m.group(1), bool(m.group(2)), m.group(4)), (m.group(3) or "").strip()


def rule_compat_kit(printers, all_printers):
    """Warn when a preset names some, but not all, kit variants of an SM2 family (model, single/dual, nozzle)."""
    fams = {}
    for p in all_printers:
        f = printer_family(p)
        if f:
            fams.setdefault(f[0], set()).add(p)
    named = set(printers)
    out = []
    for fam, members in sorted(fams.items()):
        hit = members & named
        if hit and hit != members:
            out.append(("warn:SM2-COMPAT-KIT", "lists %d of %d %s%s %s mm kit variants, missing %s"
                        % (len(hit), len(members), fam[0], " Dual" if fam[1] else "", fam[2],
                           sorted(members - hit))))
    return out


def load_orca_library(path, report):
    names = set()
    if not path:
        return names
    if not os.path.isdir(path):
        report.warn("OrcaFilamentLibrary directory not found: %s (external filament parents cannot be checked)" % path)
        return names
    for f in glob.glob(os.path.join(path, "filament", "**", "*.json"), recursive=True):
        try:
            d = load_json(f, Report())
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("name"):
            names.add(d["name"])
    log.info("Loaded %d OrcaFilamentLibrary preset names from %s", len(names), path)
    return names


def main(argv=None):
    return run(argv)[0]


def run(argv=None):
    """Validate the bundle; returns (exit status, Report)."""
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.path.dirname(here), help="repo root containing Snapmaker.json")
    default_lib = os.path.normpath(os.path.join(os.path.dirname(here), "..", "OrcaSlicer", "resources",
                                                "profiles", "OrcaFilamentLibrary"))
    ap.add_argument("--orca-filament-library", default=default_lib,
                    help="OrcaFilamentLibrary profile dir used as filament base bundle (default: %(default)s)")
    ap.add_argument("--strict", action="store_true", help="also fail on non-A250/A350 findings")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s", stream=sys.stdout)
    report = Report()
    root = os.path.abspath(args.root)
    vendor_dir = os.path.join(root, VENDOR)
    index_path = os.path.join(root, VENDOR + ".json")
    log.info("Validating %s", index_path)

    try:
        index = load_json(index_path, report)
    except (OSError, ValueError) as exc:
        report.issue(True, "cannot load %s: %s" % (index_path, exc))
        return finish(report, args.strict), report
    log.info("Bundle %s version %s", index.get("name"), index.get("version"))
    if not re.fullmatch(r"\d+\.\d+(\.\d+){0,2}(-[A-Za-z0-9]+)?(\+[A-Za-z0-9]+)?", str(index.get("version", ""))):
        report.issue(True, "Snapmaker.json version %r does not match Orca's updater regex" % index.get("version"))

    orca_lib = load_orca_library(args.orca_filament_library, report)

    # ---- pass 1: load every indexed file -------------------------------------------------------
    presets = {}  # (type, name) -> dict(data, path, pos)
    referenced = set()
    for key, ptype in LISTS:
        seen = set()
        for pos, entry in enumerate(index.get(key, [])):
            name, sub = entry.get("name"), entry.get("sub_path")
            sm2 = is_sm2_name(name)
            if name in seen:
                report.issue(sm2, "%s: duplicate entry name %r" % (key, name))
            seen.add(name)
            if not sub:
                report.issue(sm2, "%s: entry %r has no sub_path" % (key, name))
                continue
            referenced.add(sub)
            path = os.path.join(vendor_dir, sub)
            if not os.path.isfile(path):
                report.issue(sm2, "%s: %r -> missing file %s" % (key, name, sub))
                continue
            try:
                data = load_json(path, report)
            except ValueError as exc:
                report.issue(sm2, "%s: %s is not valid JSON: %s" % (key, sub, exc))
                continue
            if data.get("name") != name:
                report.issue(sm2 or is_sm2_name(data.get("name")),
                             "%s: index name %r != file name field %r (%s)" % (key, name, data.get("name"), sub))
            if data.get("type") != ptype:
                report.issue(sm2, "%s: %s has type %r, expected %r" % (key, sub, data.get("type"), ptype))
            presets[(ptype, name)] = {"data": data, "path": sub, "pos": pos}
    log.info("Indexed presets: %s", ", ".join("%s=%d" % (t, sum(1 for k in presets if k[0] == t))
                                               for _, t in LISTS))

    def get(ptype, name):
        p = presets.get((ptype, name))
        return p["data"] if p else None

    machines_inst = {n for (t, n), p in presets.items() if t == "machine" and p["data"].get("instantiation") == "true"}

    # Which presets matter for the Snapmaker 2.0? Leaves naming A250/A350 or compatible with them,
    # plus every ancestor of such a leaf (fdm_linear2, fdm_common, filament category bases, ...).
    def is_sm2_leaf(ptype, data):
        if is_sm2_name(data.get("name")) or is_sm2_name(data.get("printer_model")):
            return True
        return any(is_sm2_name(p) for p in as_list(data.get("compatible_printers")))

    sm2 = set()
    for (ptype, name), p in presets.items():
        if is_sm2_leaf(ptype, p["data"]):
            cur, guard = name, 0
            while cur and (ptype, cur) in presets and guard < 50:
                sm2.add((ptype, cur))
                cur = presets[(ptype, cur)]["data"].get("inherits")
                guard += 1
    log.info("A250/A350 scope: %d presets (incl. their bases)", len(sm2))

    # ---- pass 2: per-preset checks ---------------------------------------------------------------
    def resolve(ptype, name):
        """Merge the inherits chain (parent first); returns (config, chain)."""
        chain, cur = [], name
        while cur and (ptype, cur) in presets and cur not in chain and len(chain) < 50:
            chain.append(cur)
            cur = presets[(ptype, cur)]["data"].get("inherits")
        cfg = {}
        for n in reversed(chain):
            cfg.update(presets[(ptype, n)]["data"])
        return cfg, chain

    for (ptype, name), p in sorted(presets.items()):
        data, in_sm2, sub = p["data"], (ptype, name) in sm2, p["path"]
        if ptype == "machine_model":
            continue
        inst = data.get("instantiation")
        if inst not in ("true", "false"):
            report.issue(in_sm2, "%s: instantiation is %r (must be \"true\"/\"false\")" % (sub, inst))
        parent = data.get("inherits")
        if parent:
            pp = presets.get((ptype, parent))
            if pp is None:
                if ptype == "filament" and parent in orca_lib:
                    log.debug("%s inherits %r from OrcaFilamentLibrary", sub, parent)
                else:
                    report.issue(in_sm2, "%s: inherits %r which is not a %s preset in the bundle" % (sub, parent, ptype))
            elif pp["pos"] >= p["pos"]:
                report.issue(in_sm2, "%s: inherits %r which is listed AFTER it in %s_list (Orca loads in order)"
                             % (sub, parent, ptype))

        for printer in as_list(data.get("compatible_printers")):
            if printer not in machines_inst:
                report.issue(in_sm2 or is_sm2_name(printer),
                             "%s: compatible_printers names unknown machine preset %r" % (sub, printer))

        if inst != "true":
            continue
        cfg, chain = resolve(ptype, name)

        if ptype == "filament" and in_sm2:
            if not (cfg.get("filament_id") or cfg.get("setting_id")):
                report.issue(True, "%s: no filament_id/setting_id in the inherits chain" % sub)
            for key in ("filament_density", "filament_diameter"):
                vals = [to_float(v) for v in as_list(cfg.get(key))]
                if not vals:
                    report.issue(True, "%s: %s missing in the whole inherits chain %s" % (sub, key, chain))
                elif any(v is None or v <= 0 for v in vals):
                    report.issue(True, "%s: %s = %s (must be > 0; weight/cost estimates break)"
                                 % (sub, key, as_list(cfg.get(key))))
        elif ptype == "filament":
            for key in ("filament_density", "filament_diameter"):
                vals = [to_float(v) for v in as_list(cfg.get(key))]
                if vals and any(v is None or v <= 0 for v in vals):
                    report.issue(False, "%s: %s = %s" % (sub, key, as_list(cfg.get(key))))

        if ptype == "machine":
            model = cfg.get("printer_model")
            mdata = get("machine_model", model)
            if mdata is None:
                report.issue(in_sm2, "%s: printer_model %r is not in machine_model_list" % (sub, model))
            else:
                variants = [v.strip() for v in str(mdata.get("nozzle_diameter", "")).split(";") if v.strip()]
                if cfg.get("printer_variant") not in variants:
                    report.issue(in_sm2, "%s: printer_variant %r not in %r nozzle_diameter %s"
                                 % (sub, cfg.get("printer_variant"), model, variants))
            nozzles = [to_float(v) for v in as_list(cfg.get("nozzle_diameter"))]
            if not nozzles or any(v is None or v <= 0 for v in nozzles):
                report.issue(in_sm2, "%s: nozzle_diameter %s invalid" % (sub, cfg.get("nozzle_diameter")))
            elif cfg.get("printer_variant") and to_float(cfg.get("printer_variant")) != nozzles[0]:
                report.issue(in_sm2, "%s: printer_variant %r != nozzle_diameter %s"
                             % (sub, cfg.get("printer_variant"), cfg.get("nozzle_diameter")))
            dpp = cfg.get("default_print_profile")
            if dpp and get("process", dpp) is None:
                report.issue(in_sm2, "%s: default_print_profile %r does not exist" % (sub, dpp))
            for fil in as_list(cfg.get("default_filament_profile")):
                if fil and get("filament", fil) is None:
                    report.issue(in_sm2, "%s: default_filament_profile %r does not exist" % (sub, fil))
            spec = SM2_SPEC.get(sm2_model_of(model))
            if spec:
                xs, ys = [], []
                for pt in as_list(cfg.get("printable_area")):
                    try:
                        x, y = (float(c) for c in str(pt).split("x"))
                    except ValueError:
                        report.issue(True, "%s: bad printable_area point %r" % (sub, pt))
                        continue
                    xs.append(x)
                    ys.append(y)
                if not xs:
                    report.issue(True, "%s: printable_area missing" % sub)
                elif min(xs) < 0 or min(ys) < 0 or max(xs) > spec[0] or max(ys) > spec[1]:
                    report.issue(True, "%s: printable_area %s exceeds %s spec %gx%g mm"
                                 % (sub, cfg.get("printable_area"), sm2_model_of(model), spec[0], spec[1]))
                h = to_float(cfg.get("printable_height"))
                if h is None or h <= 0:
                    report.issue(True, "%s: printable_height %r invalid" % (sub, cfg.get("printable_height")))
                elif h > spec[2]:
                    report.issue(True, "%s: printable_height %g exceeds %s spec %g mm"
                                 % (sub, h, sm2_model_of(model), spec[2]))
                else:
                    log.debug("%s: area max %gx%g, height %g (spec %gx%gx%g)",
                              sub, max(xs), max(ys), h, *spec)
                    if h < spec[2] and "(0.4 nozzle)" in name:
                        report.info("%s: printable_height %g mm (spec %g; %g mm margin/kit allowance)"
                                    % (name, h, spec[2], spec[2] - h))

    check_sm2_rules(presets, resolve, machines_inst, sm2, report)

    # ---- pass 3: orphans -------------------------------------------------------------------------
    for sub_dir in ("machine", "filament", "process"):
        for f in sorted(glob.glob(os.path.join(vendor_dir, sub_dir, "*.json"))):
            rel = os.path.relpath(f, vendor_dir).replace(os.sep, "/")
            if rel in referenced or rel in RULE_FILES:
                continue
            try:
                d = load_json(f, report)
            except ValueError as exc:
                report.issue(is_sm2_name(rel), "%s is not valid JSON and not indexed: %s" % (rel, exc))
                continue
            orphan_sm2 = is_sm2_name(rel) or is_sm2_leaf(d.get("type"), d)
            report.issue(orphan_sm2, "%s exists on disk but is not referenced from Snapmaker.json" % rel)
    for rel in sorted(RULE_FILES):
        if not os.path.isfile(os.path.join(vendor_dir, rel)) and rel != "filament/filaments_colours.json":
            report.issue(True, "required rule file %s missing (Orca build fails)" % rel)

    return finish(report, args.strict), report


def check_sm2_rules(presets, resolve, machines_inst, sm2_scope, report):
    """Run the SM2-* content rules. Each finding is reported once per file that defines the offending value."""
    seen = set()

    def emit(findings, where, sm2=True):
        for rule, msg in findings:
            key = (rule, where, msg)
            if key in seen:
                continue
            seen.add(key)
            if rule.startswith("warn:"):
                report.warn("%s %s" % (where, msg), rule=rule[5:])
            else:
                report.issue(sm2, "%s %s" % (where, msg), rule=rule)

    def defined_in(ptype, chain, key):
        for n in chain:
            if key in presets[(ptype, n)]["data"]:
                return presets[(ptype, n)]["path"]
        return chain[0] if chain else "?"

    sm2_printers = sorted(n for n in machines_inst if presets[("machine", n)]["data"] and
                          (("machine", n) in sm2_scope))
    machine_cfg = {}
    for name in sm2_printers:
        cfg, chain = resolve("machine", name)
        machine_cfg[name] = cfg
        single = " Dual" not in name
        for rule, msg in rule_limits(cfg):
            k = "machine_max_acceleration_z" if "LIM-Z" in rule else "machine_max_speed_" + msg.split("=")[0][-1]
            emit([(rule, msg)], defined_in("machine", chain, k))
        for key in GCODE_KEYS:
            text = cfg.get(key)
            if not text:
                continue
            where = "%s:%s" % (defined_in("machine", chain, key), key)
            emit(rule_gc_mcode(text) + rule_gc_bedtype(text), where)
            if key == "machine_start_gcode":
                emit(rule_gc_hotbed(text) + rule_gc_flush(text, single), where)
                emit([(r, "%s: %s" % (name, msg)) for r, msg in rule_gc_purge(text, cfg)], where)
            if key == "change_filament_gcode":
                emit(rule_gc_bedmin(text), where)
        # outside the GCODE_KEYS loop: that loop skips empty values, and a missing pause G-code is the bug
        emit(rule_gc_pause(cfg.get("machine_pause_gcode")),
             "%s:machine_pause_gcode" % defined_in("machine", chain, "machine_pause_gcode"))
        for ptype, key in (("filament", "default_filament_profile"), ("process", "default_print_profile")):
            for dflt in as_list(cfg.get(key)):
                if not dflt or ("process" if ptype == "process" else "filament", dflt) not in presets:
                    continue  # existence is checked in pass 2
                dcfg, _ = resolve(ptype, dflt)
                compat = as_list(dcfg.get("compatible_printers"))
                if compat and name not in compat:
                    alts = sorted(n for (t, n), p in presets.items() if t == ptype
                                  and p["data"].get("instantiation") == "true"
                                  and name in as_list(resolve(ptype, n)[0].get("compatible_printers")))
                    emit([("SM2-DEF-COMPAT", "default %r not compatible; alternatives: %s"
                           % (dflt, ", ".join(alts[:4]) or "none"))], "%s %s" % (name, key))

    # other printers (U1/J1/Artisan): only the label-vs-value rule, reported as OTHER
    for name in sorted(machines_inst - set(sm2_printers)):
        cfg, chain = resolve("machine", name)
        for key in GCODE_KEYS:
            if cfg.get(key):
                emit(rule_gc_bedtype(cfg[key]), "%s:%s" % (defined_in("machine", chain, key), key), sm2=False)

    for base_a, base_b in SM2_TWINS:
        if ("machine", base_a) not in presets or ("machine", base_b) not in presets:
            continue
        cfg_a, _ = resolve("machine", base_a)
        cfg_b, _ = resolve("machine", base_b)
        for key in DRIFT_KEYS:
            emit(rule_gc_drift(base_a, cfg_a.get(key), base_b, cfg_b.get(key), key),
                 presets[("machine", base_b)]["path"])

    for (ptype, name), p in sorted(presets.items()):
        if ptype not in ("filament", "process") or p["data"].get("instantiation") != "true":
            continue
        cfg, chain = resolve(ptype, name)
        printers = [x for x in as_list(cfg.get("compatible_printers")) if x in machine_cfg]
        if not printers:
            continue
        emit(rule_compat_kit(printers, sm2_printers), p["path"])
        if ptype != "filament":
            continue
        emit(rule_temp_max(cfg, printers), p["path"])
        rs = first_float(cfg.get("filament_retraction_speed"))
        if rs is not None:
            emax = min(first_float(machine_cfg[x].get("machine_max_speed_e")) or 1e9 for x in printers)
            if rs > emax:
                emit([("warn:SM2-RETRACT-E", "filament_retraction_speed=%g > machine_max_speed_e %g (firmware"
                                             " caps E feedrate; Orca's time estimate is off)" % (rs, emax))],
                     defined_in(ptype, chain, "filament_retraction_speed"))


def finish(report, strict):
    print()
    if report.rule_counts:
        print("RULES: " + ", ".join("%s=%d" % kv for kv in sorted(report.rule_counts.items())))
    print("SUMMARY: %d A250/A350 error(s), %d other-printer issue(s), %d warning(s), %d info"
          % (len(report.errors), len(report.other), len(report.warnings), len(report.infos)))
    failed = bool(report.errors) or (strict and bool(report.other))
    print("RESULT: %s" % ("FAIL" if failed else "PASS (A250/A350 subset)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
