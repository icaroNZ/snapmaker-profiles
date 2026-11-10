# CLAUDE.md - snapmaker-profiles

## Purpose
Snapmaker's vendor preset bundle for **Snapmaker Orca** (Snapmaker's OrcaSlicer fork): OrcaSlicer/BambuStudio-format JSON
machine, process and filament presets plus four Snapmaker filament rule files. OrcaSlicer consumes it as the git
submodule `submodules/Profiles` and mirrors it into `resources/profiles/` at CMake time. It continues the older
`Orca_Presets` repo. Upstream work is mostly Snapmaker U1; the Snapmaker 2.0 (A250/A350) presets come from the macdylan
community profiles and are the ones used for the owner's **A250T**.

## Tech stack
- Pure JSON data (no build). Most preset files use **CRLF** line endings; some start with a UTF-8 BOM upstream.
- `scripts/validate_profiles.py` - Python 3 stdlib-only static validator (added on a250t), with SM2 content rules.
- `scripts/test_validate_profiles.py` - stdlib `unittest` suite for the validator and the SM2 preset fixes.
- `scripts/render_start_gcode.py` - stdlib subset of Orca's PlaceholderParser: renders a printer's start G-code for a
  given first-layer footprint (`python3 -I scripts/render_start_gcode.py "Snapmaker A250 (0.4 nozzle)" --footprint
  90,100,140,150 [--extruder 1] [--mvs 2] [--moves] [--adaptive] [--margin 5]`; `--moves` = only the `; SM2_*` comments
  and moves; `--adaptive` = add the PR-23 opt-in token to `printer_notes`). Supports Orca's `=~`/`!~` regex match. Used by
  rules SM2-GC-PURGE, SM2-GC-PARK and SM2-GC-MESH; matched real OrcaSlicer 2.5.0-beta CLI output line for line on 4
  reference slices (PR-09), on all start-section G0/G1 lines of 6 slices (smart park) and on the `G1029` line of 7
  adaptive-mesh slices (PR-23).
- No CI here; OrcaSlicer's `.github/workflows/check_profiles.yml` checks this bundle after syncing it.
- No licence file, no README, no tags.

## Build and test (macOS, verified 07/11/2026, re-run 10/11/2026 on uncommitted 02.03.04.12+a250t)
```sh
cd /Users/icaro/Documents/Projects/SnapMaker/snapmaker-profiles
python3 -I scripts/validate_profiles.py            # RESULT: PASS (A250/A350 subset), exit 0
python3 -I scripts/validate_profiles.py -v         # + geometry details
python3 -I scripts/validate_profiles.py --strict   # exit 1: the 9 "other" (U1) issues below
python3 -I -B scripts/test_validate_profiles.py    # 82 tests, OK (~3 s; copies the bundle to a temp dir; -B: no __pycache__)
```
Current result (10/11/2026, 02.03.04.12+a250t, uncommitted): `0 A250/A350 error(s), 9 other-printer issue(s), 33 warning(s),
16 info`, plus a `RULES:` line with counts per rule ID. Other = upstream U1: 4x `default_print_profile` missing,
1 duplicate `Snapmaker PLA SnapSpeed @U1 base`, 4x `SM2-GC-BEDTYPE` (U1 start G-code compares `curr_bed_type` with
the UI label "Smooth PEI Plate", upstream PR2). Warnings (known, not fixed): `SM2-LIM-XY` x2 (XY 150 > firmware 120,
needs T-module check), `SM2-RETRACT-E` x25 (filament retraction 60 > E max 45; firmware caps it), `SM2-TEMP-MAX` x4
(ABS/ASA single `range_high` 280 > 275; nothing prints at it), `SM2-COMPAT-KIT` x2 (PVA @0.2 / PET @Dual 0.8 list only
the plain printer of their family). The 16 INFO lines (printable_height 5 mm under spec, lower for kits) are
intentional.
SM2 rules (IDs in `validate_profiles.py` docstring, firmware facts as constants at the top): SM2-LIM-Z, SM2-LIM-XY,
SM2-TEMP-MAX, SM2-RETRACT-E, SM2-DEF-COMPAT, SM2-COMPAT-KIT, SM2-GC-MCODE (allowlist from controller `gcode.cpp`),
SM2-GC-DRIFT (A250 vs A350 twin templates), SM2-GC-BEDTYPE, SM2-GC-HOTBED, SM2-GC-FLUSH, SM2-GC-BEDMIN (Orca #552),
SM2-GC-PAUSE (every A250/A350 printer's `machine_pause_gcode` must resolve to a template with an `M600` line, PR-01),
SM2-GC-PURGE (start G-code uses `first_layer_print_min/max`, `print_bed_min/max`, `nozzle_diameter[`,
`filament_max_volumetric_speed[`; rendered for 9 sample footprints per printer, T0/T1 on Dual: all G0/G1 on the bed and
outside `bed_exclude_area`, purge >= 4 mm from the footprint unless fallback, F <= 3420, flow <= MVS; PR-09),
SM2-GC-PARK (start G-code builds `sm2_park_x/y` from `sm2_purge_x/y/sx/sy`; rendered for 11 footprints: the first XY
move after G28 and the flush blob sit on the `; SM2_PARK` point, which is on the bed, X >= 10 mm inside, outside
`bed_exclude_area`, >= 10 mm and no closer than the purge start to the footprint, >= 8 mm from every purge extrusion,
<= 15 mm from the purge line; fallback exempt; smart park). PURGE and PARK share the footprint list (9 + 2 clamp cases).
SM2-GC-MESH (PR-23, same footprints at `adaptive_bed_mesh_margin` 0 and 5 mm: no `G1029` unless `printer_notes`
contains `SM2_ADAPTIVE_MESH`; with it single-head printers emit exactly one `G1029 A L.. R.. F.. B..` (no H/K/P) after
`G28` and `M190 R` and before every G0/G1, region = `adaptive_bed_mesh_min/max` clamped to the bed, nothing else changes;
Dual printers never emit `G1029`).
Machine-test fixes (10/11/2026): SM2-GC-SCREENTEMP (F1, `layer_change_gcode` writes sacrificial + real M140 (single:
also M104) on `layer_num == 1` only, no waits; Orca's own layer-2 M104/M140 still follow), SM2-CFG (F6 `host_type snapmaker2`, F11
`disable_m73 1`, F8 `machine_max_junction_deviation` in 0.01-0.3); SM2-GC-FLUSH finds flush lines by `first_layer + 15`.
The `RULES:` line lists only rules with findings.
Orca's CI checker must run on a scratch copy (it reads `<script>/../resources/profiles`): copy
`OrcaSlicer/scripts/orca_extra_profile_check.py` to `<S>/scripts/`, `OrcaSlicer/resources/profiles/OrcaFilamentLibrary*`
and this repo's `Snapmaker/` + `Snapmaker.json` to `<S>/resources/profiles/`, then
`python3 -I scripts/orca_extra_profile_check.py --vendor Snapmaker --check-filaments --check-materials` -> 0 errors,
0 warnings. Full recipe: workspace skill `sm-profiles-validate`. A real check in the app needs an OrcaSlicer build
(see `_snapmaker2-review/integration/OrcaSlicer.md`).

## Directory map
| Path | Purpose |
|---|---|
| `Snapmaker.json` | Bundle index. `version` (line 3, `02.03.04.12+a250t`), `machine_model_list`, `process_list` (:84), `filament_list` (:534), `machine_list` (:1788). Each entry `{name, sub_path}`. |
| `Snapmaker/machine/` (119) | `Snapmaker <Model>.json` (type `machine_model`), `fdm_*` abstract bases, `Snapmaker <Model> (<n> nozzle).json` printers. |
| `Snapmaker/process/` (112) | `fdm_process_*` bases; `0.xx <Quality> @Snapmaker[ J1| Artisan| U1] (<n> nozzle).json`. SM2 uses plain `@Snapmaker`. |
| `Snapmaker/filament/` (316) | `fdm_filament_common`, `fdm_filament_<type>_category` bases; `Snapmaker <type>[ @Dual][ @U1][ @0.x nozzle]`, `Generic ...`, Polymaker. |
| `Snapmaker/filament/filament_allow_list.json` | Rule file: high-flow not-recommended/unavailable lists, `filament_order`. |
| `Snapmaker/filament/filament_compatibility.json` | Rule file: filament mixing compatibility. |
| `Snapmaker/filament/filament_hot_bed_nozzles.json` | Rule file: plate (btPEI, btGESP, ...) / nozzle restrictions per filament. |
| `Snapmaker/filament/filaments_colours.json` | Rule file: official colour SKUs. |
| `scripts/validate_profiles.py` | Validator (a250t). |
| `scripts/test_validate_profiles.py` | Validator/preset tests (a250t). |
| `scripts/render_start_gcode.py` | Offline Orca template renderer + CLI (a250t, PR-09; regex + adaptive-mesh variables PR-23). |

## Key files for the Snapmaker 2.0
- Chain: `Snapmaker A250 (0.4 nozzle).json` -> `fdm_a250.json` -> `fdm_linear2.json` -> `fdm_common.json`.
  Dual: `fdm_a250_dual.json` -> `fdm_linear2_dual.json` -> `fdm_linear2.json`. Kits: `fdm_a250_qs|bk|qs_bk.json` ->
  `fdm_a250.json`; `fdm_a250_dual_qs|bk|qs_bk.json` -> `fdm_a250_dual.json`. Identical structure for A350.
- `fdm_common.json`: `gcode_flavor marlin2`, retraction 0.8 mm @45 mm/s, z_hop 0.4. Its `pause_gcode "M600 ;pause print"`
  (:7) is a legacy key that Orca does not know and drops on load: it is unused. Orca's "Pause at layer" writes
  `machine_pause_gcode` after `;PAUSE_PRINT` (OrcaSlicer `GCode.cpp:5011-5018`), which PR-01 sets to `M600` on
  `fdm_a250`, `fdm_a350`, `fdm_a250_dual` and `fdm_a350_dual` (kit variants inherit; not on `fdm_linear2`/`fdm_common`,
  which Artisan/J1 also use).
- `fdm_linear2.json`: SM2 limits (accel X1100/Y1000/Z100 = firmware Z, BK bases no longer override Z; speed 150/150/40 mm/s, jerk 5/1/1/3), thumbnails `300x150`.
- `fdm_a250.json:9-14`: `printable_area` 230x250, `printable_height` 230, `printer_notes PRINTER_MODEL_SNAPMAKER_A250`,
  `machine_pause_gcode` (:12, `M600`; F10 is fixed in the controller a250t.12, not here), `host_type snapmaker2`, `disable_m73 1`,
  `machine_max_junction_deviation 0.02`, `layer_change_gcode` (F1), start/end G-code (`; Update: 20250313`). Start
  G-code: flush temp single `min(275, min(nozzle_temperature_range_high, first_layer + 15))` (F3), Dual
  `min(290, min(nozzle_temperature_range_high, max(250, first_layer + 15)))`; hot-bed +0.06 mm
  (bed >= 90 C) applied as `G0 Z1.66` + `G92 Z1.6` (`; SM2_HOTBED_COMP` comment), never at Z0.06 near the bed.
  Purge (PR-09): `{local sm2_*}` lines right after the header pick a 100 mm two-pass line in front of the first-layer
  footprint, else left, else right (15/10 mm from it, X 10 mm inside the bed, never behind it), else the old X40 Y0
  180 mm line; width 1.25 x nozzle at Z0.3, feed min(3420, MVS-limited), primes E4/E3 capped by MVS;
  `sm2_purge_x/y` = first-pass start; output comment `; SM2_PURGE <side> start X.. Y..`.
  Smart park: `sm2_park_*` locals put the flush wait (`M109 ... C2 W1` at Z1.6) and the `G1 E4` blob 10 mm behind the
  purge start along the line and 10 mm further from the part, X clamped to 10 mm inside the bed, Y >= bed min; if the
  clamps leave it < 10 mm from the purge start it goes 10 mm past the first pass end; fallback keeps X10 Y0. Then the
  old slow lift (Z5.6 F200, fan on), travel, Z0.3, `M109 ... C3 W1` at the purge start. Comment
  `; SM2_PARK start|end X.. Y..`. `; SM2_MESH` comment right after `M190 R..`; the SM2 `G28` already re-enables the
  saved mesh (controller `G28.cpp:486-494`). Adaptive mesh (PR-23, single-head `fdm_a250`/`fdm_a350` only, OPT-IN):
  directly after that comment `{if printer_notes=~/.*SM2_ADAPTIVE_MESH.*/}` emits `; SM2_MESH_ADAPTIVE on ...` and
  `G1029 A L.. R.. F.. B..` = Orca `adaptive_bed_mesh_min/max` (first-layer bbox incl. skirt -/+ `adaptive_bed_mesh_margin`)
  clamped to `print_bed_min/max`; no H/K (bed already hot; H would undo the screen's bed-temperature override). Turn on:
  Printer settings > Notes, add a line `SM2_ADAPTIVE_MESH`, save as a user preset. Needs controller SM2-5.2.1-a250t.8+
  (older firmware ignores L/R/F/B and runs a full `G1029 A`: stock/a250t.3 leave a raw mesh for a card step a print never
  does). Without the token the output is byte-identical to 02.03.04.10. Dual bases: no line (controller refuses
  `G1029 A` without the single 3DP head, `G1029.cpp:337-340`). Kits: BK 224, QS 215, QS+BK 209, Dual 190, Dual BK 184, Dual QS 175,
  Dual QS+BK 169 mm; QS/Dual add `bed_exclude_area`.
- `fdm_a250_dual.json:25`: `change_filament_gcode` (T{next}, `M107 P`, `M109 T.. C3 W1`; header comment
  `; Change T<a> -> T<b> layer <n> z <z>`). On layer_num 1 (0-based = 2nd layer) with PLA/TPU it sets
  `M140 S{max(bed a, bed b)}` (was min(), Orca #552). Dual start G-code preheats the idle nozzle to
  first layer + `standby_temperature_delta` only when the process option Ooze prevention is on; Orca then also emits
  the standby `M104 ... ;cooldown` at toolchanges. Dual `default_filament_profile` = `Snapmaker PLA @Dual` x2.
- Model file `Snapmaker A250.json`: `url https://github.com/macdylan`, nozzles `0.2;0.4;0.6;0.8`,
  `bed_model "Snapmaker A250_bed.stl"` (asset stored in OrcaSlicer, not here).
- Process: `fdm_process_common.json` + 7 shared 0.4 mm processes (default `0.16 Optimal @Snapmaker (0.4 nozzle)`);
  0.8 mm default `0.24 Standard @Snapmaker (0.8 nozzle)` (re-indexed on a250t).
- Filaments: 58 files list an A250 printer (`Snapmaker PLA*`, PETG/PETG-CF, PET, ABS, ASA, PA-CF, PVA, TPU, TPE,
  Breakaway, PolyLite/PolyTerra PLA, with `@Dual`, `@0.2 nozzle`, `@Dual 0.8 nozzle` variants).

## Conventions seen in the data
- Every preset: `type`, `name` (== filename without `.json` == index name), `from: "system"`, `instantiation`
  ("true"/"false" strings), `inherits`, and `setting_id` on instantiated presets. All values are strings or string
  arrays (`"230"`, `["0.4"]`); per-extruder keys are arrays (Dual = 2 entries). `nil` in a filament array = "use the
  printer value".
- Names: printers `Snapmaker <Model> (<d> nozzle)`; processes `<layer> <Quality> @Snapmaker (<d> nozzle)`; filaments
  `Snapmaker <type>[ @Dual][ @<d> nozzle]`.
- Index order is load order: parent entries must come before children in the same list (Orca aborts the whole vendor
  otherwise). Rule files are never indexed.
- Upstream commits are Chinese one-liners ("更新...至版本02.03.04.04") and bump `version` last field per release;
  bot commits from "ProfilesCMS" (branch `origin/02.03.05.00`) carry `ProfilesCMS-Operation:` trailers.
- G-code strings are single JSON lines with `\n`, Orca placeholders `[x]` / `{expr}`; macdylan `SMFix` marker block at
  the end of `machine_end_gcode` must stay.
- Validator: Python `logging` (`ERROR [A250/A350]`, `WARNING [other]`, `WARNING [warn]`, `INFO [info]`), the rule
  ID right after the tag (e.g. `ERROR [A250/A350] SM2-LIM-Z ...`), per-rule counts and summary in `finish()`.

## How to extend
- **Add/change a filament or process**: copy a sibling, keep only differing keys, set `compatible_printers` to exact
  printer names, add `{name, sub_path}` to `Snapmaker.json` after its parent, validate, bump version. Skill
  `sm-profiles-add-preset`.
- **Add a printer/nozzle variant or an A250T model**: new `machine_model` + `fdm_a250t` base (inherit `fdm_a250`) +
  nozzle printers + add the names to `compatible_printers` of ~22 processes and ~58 filaments; Orca's
  `CreatePresetsDialog.cpp:140-143` lists model names (OrcaSlicer side). Same skill.
- **Change start/end/toolchange G-code**: edit `fdm_a250*.json` / `fdm_a350*.json` (all variants that override the key).
  Use only commands the SM2 controller handles (Marlin 2 + `M109 C/W`).
- **Release it**: bump `Snapmaker.json` version (`02.03.04.0N+a250t`, keep `+`), then OrcaSlicer gitlink. Skill
  `sm-profiles-ship-to-orca`.

## A250T notes
- No A250T (or A150, or T-series) preset exists; use `Snapmaker A250 ...` (`Dual` for the dual extruder module,
  `QSKit`/`BKit`/`QS+B Kit` if those kits are fitted). Bed 230x250 mm, height 230 mm (single).
- Nothing here encodes TMC linear modules; motion limits in `fdm_linear2.json` are the generic SM2 ones.
- Laser and CNC are not covered by Orca at all (use Luban). Snapmaker Orca upstream cannot connect to an SM2; the
  a250t OrcaSlicer adds a Snapmaker 2 print host (OrcaSlicer side, not here).
- The installed app (`~/Library/Application Support/Snapmaker_Orca/system/Snapmaker.json`) is at official
  `02.03.04.04` on 07/11/2026; a250t fixes appear only in a self-built Orca.

## a250t/main changes (on top of upstream main 1e201f6)
17bf0bb validator; c7757d9 SnapSpeed `filament_density` 1.25; 252a7a2 re-index `0.24 Standard @Snapmaker (0.8 nozzle)`
(default of all 16 SM2 0.8 mm printers); 3ff1148 version 02.03.04.05+a250t; 497718b J1 `default_filament_profile`
-> `Snapmaker PLA @J1`/`Snapmaker PETG @J1`, BOM removed from `fdm_process_common.json` and one U1 process;
f0d147d version 02.03.04.06+a250t; 0655774 restore CRLF in `fdm_idex.json`; 0f53633 restore CRLF in `Snapmaker.json`.
Fix pass 07/11/2026: 2a66478 SM2 validator rules; 019985e Z accel 100 (SP-02); df91431 hot-bed offset at Z1.6 (SP-01);
8b44414 flush temp cap (SP-03); b24a728 Dual toolchange bed max() (Orca #552); 423eed9 Dual idle preheat behind Ooze
prevention (SP-06); 8923d1e Snapmaker PET 275 (SP-04); 1421de3 Dual default `Snapmaker PLA @Dual` (SP-05); f3b89cd
A350 Dual purge E12; a8d00ff tests; 440f22a version 02.03.04.07+a250t; 2b71728 CLAUDE.md.
PR-01 08/11/2026: 12a494f `machine_pause_gcode` M600 on the four SM2 bases + rule SM2-GC-PAUSE + tests (27); e31798c
version 02.03.04.08+a250t (pushed to the icaroNZ fork).
PR-09 08/11/2026: 2c1c239 `render_start_gcode.py`; 3d8a9f8 footprint/nozzle/MVS-aware purge in the four SM2 start
templates + rule SM2-GC-PURGE + tests (44); 6dba6d3 version 02.03.04.09+a250t (local, not pushed; gitlink not moved).
Smart park 08/11/2026: df0f9fc flush/park point derived from the purge start + rule SM2-GC-PARK + `--moves` + tests (56);
8ee9b52 version 02.03.04.10+a250t (local, not pushed; gitlink not moved).
PR-23 08/11/2026: f77ad16 opt-in adaptive `G1029 A L R F B` after `M190 R` (single-head bases, `printer_notes` token
`SM2_ADAPTIVE_MESH`) + renderer regex/`--adaptive` + rule SM2-GC-MESH + tests (71); b9210bb version 02.03.04.11+a250t
(local, not pushed; gitlink not moved).
Machine-test fixes 10/11/2026 (UNCOMMITTED): F1 sacrificial layer-2 M104/M140 (screen 165/70 rewrite), F3 single
flush first layer + 15, F6 host_type snapmaker2, F8 junction deviation 0.02,
F11 disable_m73; rules SM2-GC-SCREENTEMP/CFG; tests 82; version 02.03.04.12+a250t. F10 (pause temperature) was tried as a
pause-G-code trick and dropped after review on 10/11/2026: the controller a250t.12 restores the pre-pause target itself.
Fix list:
`../_snapmaker2-review/issues/snapmaker-profiles.md`. Details:
`../_snapmaker2-review/integration/snapmaker-profiles.md`.

## Cross-repo interfaces
Full map: `../.claude/knowledge/DEPENDENCIES.md`; facts: `../.claude/knowledge/repos/snapmaker-profiles.json`;
specialist agent: `sm-profiles`.
- **OrcaSlicer** (sm-orca): `.gitmodules` `submodules/Profiles` -> `https://github.com/icaroNZ/snapmaker-profiles.git`
  branch `a250t/main`, gitlink e31798c (moved in OrcaSlicer 8df381cf21 on 08/11/2026, owner's OK); `CMakeLists.txt:76-93` + `cmake/SyncSnapmakerProfiles.cmake` (requires
  `Snapmaker.json` + 3 rule files, mirrors machine/filament/process); `check_profiles.yml`; `PresetBundle.cpp`
  (`SM_BUNDLE = "Snapmaker"`), `PresetUpdater.cpp:919,1349-1368,1407-1413` (version regex, rule-file deploy,
  resource-vs-installed compare); rule readers `AllowlistManager.cpp:15`, `FilamentColorLibrary.cpp:111`,
  `FilamentHotBedNozzleRules.cpp:24-32`, `GUI/MixedColorMatchHelpers.cpp:1062-1080`. Bed STL/SVG/cover PNGs live in
  `OrcaSlicer/resources/profiles/Snapmaker/`.
- **Snapmaker2-Controller** (sm-controller): executes the emitted G-code; `M109 C/W` =
  `Marlin/src/gcode/temperature/M104_M109.cpp:140-146` (`ENABLE_CUSTOM_M109_PARAM`, `module/temperature.h:664`).
- **snapmaker-version-center** (sm-version-center): profile OTA manifest `upgrade/profile/<lang>/version.json`, fetched
  from `https://meta-cfg.snapmaker.com` (`OrcaSlicer/src/libslic3r/AppConfig.cpp:43,47,1433`).
- **Orca_Presets** (sm-orca-presets): predecessor; shares history up to 59390ad.
- Not used by Luban (luban-print-settings), the Cura plugin or Snapmaker-3D-Printing-Profiles.

## What NOT to do
- Never push (remotes: `origin` = Snapmaker, `icaro` = icaroNZ fork; the user pushes). Never change remotes.
- Do not change OrcaSlicer's `submodules/Profiles` gitlink from this repo's work unless explicitly asked; it is an
  OrcaSlicer commit.
- **Keep line endings.** Most presets are CRLF. Two a250t commits converted files by accident (497718b `fdm_idex.json`,
  fixed in 0655774; f0d147d turned `Snapmaker.json` from CRLF to LF - restored to CRLF in 0f53633). Use Edit, not
  `json.dump`; check `grep -c $'\r' <file>` before and `git diff --stat` after (a whole-file diff = broken endings).
- Do not re-serialise or pretty-print files; do not reorder index lists (order = load order).
- Do not `git add .` or `git add -A`: the root `.DS_Store` is untracked. `Snapmaker/.DS_Store` is tracked by mistake
  (added in 497718b, absent on upstream main); leave it unless asked to clean up.
- Do not "fix" printable heights to the spec maximum (deliberate margin; firmware Z limits).
- Do not edit `OrcaSlicer/resources/profiles/Snapmaker/{machine,filament,process}` - the CMake sync overwrites them.
- Do not write into `~/Library/Application Support/Snapmaker_Orca/` without explicit approval and a backup.
- Do not bump `version` with `-suffix` or change the `02.03` major/minor (breaks Orca's upgrade compare).
- Commit on `a250t/main` only, message ending with a blank line and
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
