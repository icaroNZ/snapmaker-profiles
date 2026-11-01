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
  referenced from ``Snapmaker.json`` (orphans).

Findings that involve the Snapmaker 2.0 A250/A350 presets (directly, or as a
base of them) are ERRORs and make the script exit with status 1. Findings on
other printers (U1, J1, Artisan) are reported as OTHER and do not fail the
run unless ``--strict`` is given.

Usage:  python3 scripts/validate_profiles.py [--root DIR] [--orca-filament-library DIR] [--strict] [-v]
"""

import argparse
import glob
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


class Report:
    def __init__(self):
        self.errors, self.other, self.warnings, self.infos = [], [], [], []

    def issue(self, sm2, msg):
        if sm2:
            self.errors.append(msg)
            log.error("[A250/A350] %s", msg)
        else:
            self.other.append(msg)
            log.warning("[other] %s", msg)

    def warn(self, msg):
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
        return finish(report, args.strict)
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

    return finish(report, args.strict)


def finish(report, strict):
    print()
    print("SUMMARY: %d A250/A350 error(s), %d other-printer issue(s), %d warning(s), %d info"
          % (len(report.errors), len(report.other), len(report.warnings), len(report.infos)))
    failed = bool(report.errors) or (strict and bool(report.other))
    print("RESULT: %s" % ("FAIL" if failed else "PASS (A250/A350 subset)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
