# CLAUDE.md - snapmaker-profiles

## Purpose
Snapmaker's vendor preset bundle for **Snapmaker Orca** (Snapmaker's OrcaSlicer fork): OrcaSlicer/BambuStudio-format JSON
machine, process and filament presets plus four Snapmaker filament rule files. OrcaSlicer consumes it as the git
submodule `submodules/Profiles` and mirrors it into `resources/profiles/` at CMake time. It continues the older
`Orca_Presets` repo. Upstream work is mostly Snapmaker U1; the Snapmaker 2.0 (A250/A350) presets come from the macdylan
community profiles and are the ones used for the owner's **A250T**.

## Tech stack
- Pure JSON data (no build). Most preset files use **CRLF** line endings; some start with a UTF-8 BOM upstream.
- `scripts/validate_profiles.py` - Python 3 stdlib-only static validator (added on a250t).
- No CI here; OrcaSlicer's `.github/workflows/check_profiles.yml` checks this bundle after syncing it.
- No licence file, no README, no tags.

## Build and test (macOS, verified 07/11/2026)
```sh
cd /Users/icaro/Documents/Projects/SnapMaker/snapmaker-profiles
python3 -I scripts/validate_profiles.py            # RESULT: PASS (A250/A350 subset), exit 0
python3 -I scripts/validate_profiles.py -v         # + geometry details
python3 -I scripts/validate_profiles.py --strict   # exit 1: 4 U1 default_print_profile + 1 duplicate (upstream issues)
```
Current result: `0 A250/A350 error(s), 5 other-printer issue(s), 0 warning(s), 16 info`. The 16 INFO lines
(printable_height 5 mm under spec, lower for kits) are intentional.
Orca's CI checker must run on a scratch copy (it reads `<script>/../resources/profiles`): copy
`OrcaSlicer/scripts/orca_extra_profile_check.py` to `<S>/scripts/`, `OrcaSlicer/resources/profiles/OrcaFilamentLibrary*`
and this repo's `Snapmaker/` + `Snapmaker.json` to `<S>/resources/profiles/`, then
`python3 -I scripts/orca_extra_profile_check.py --vendor Snapmaker --check-filaments --check-materials` -> 0 errors,
0 warnings. Full recipe: workspace skill `sm-profiles-validate`. A real check in the app needs an OrcaSlicer build
(see `_snapmaker2-review/integration/OrcaSlicer.md`).

## Directory map
| Path | Purpose |
|---|---|
| `Snapmaker.json` | Bundle index. `version` (line 3, `02.03.04.06+a250t`), `machine_model_list`, `process_list` (:84), `filament_list` (:534), `machine_list` (:1788). Each entry `{name, sub_path}`. |
| `Snapmaker/machine/` (119) | `Snapmaker <Model>.json` (type `machine_model`), `fdm_*` abstract bases, `Snapmaker <Model> (<n> nozzle).json` printers. |
| `Snapmaker/process/` (112) | `fdm_process_*` bases; `0.xx <Quality> @Snapmaker[ J1| Artisan| U1] (<n> nozzle).json`. SM2 uses plain `@Snapmaker`. |
| `Snapmaker/filament/` (316) | `fdm_filament_common`, `fdm_filament_<type>_category` bases; `Snapmaker <type>[ @Dual][ @U1][ @0.x nozzle]`, `Generic ...`, Polymaker. |
| `Snapmaker/filament/filament_allow_list.json` | Rule file: high-flow not-recommended/unavailable lists, `filament_order`. |
| `Snapmaker/filament/filament_compatibility.json` | Rule file: filament mixing compatibility. |
| `Snapmaker/filament/filament_hot_bed_nozzles.json` | Rule file: plate (btPEI, btGESP, ...) / nozzle restrictions per filament. |
| `Snapmaker/filament/filaments_colours.json` | Rule file: official colour SKUs. |
| `scripts/validate_profiles.py` | Validator (a250t). |

## Key files for the Snapmaker 2.0
- Chain: `Snapmaker A250 (0.4 nozzle).json` -> `fdm_a250.json` -> `fdm_linear2.json` -> `fdm_common.json`.
  Dual: `fdm_a250_dual.json` -> `fdm_linear2_dual.json` -> `fdm_linear2.json`. Kits: `fdm_a250_qs|bk|qs_bk.json` ->
  `fdm_a250.json`; `fdm_a250_dual_qs|bk|qs_bk.json` -> `fdm_a250_dual.json`. Identical structure for A350.
- `fdm_common.json`: `gcode_flavor marlin2`, `pause_gcode "M600 ;pause print"`, retraction 0.8 mm @45 mm/s, z_hop 0.4.
- `fdm_linear2.json`: SM2 limits (accel X1100/Y1000/Z500, speed 150/150/40 mm/s, jerk 5/1/1/3), thumbnails `300x150`.
- `fdm_a250.json:9-13`: `printable_area` 230x250, `printable_height` 230, `printer_notes PRINTER_MODEL_SNAPMAKER_A250`,
  start/end G-code (`; Update: 20250313`). Kits: BK 224, QS 215, QS+BK 209, Dual 190, Dual BK 184, Dual QS 175,
  Dual QS+BK 169 mm; QS/Dual add `bed_exclude_area`.
- `fdm_a250_dual.json:25`: `change_filament_gcode` (T{next}, `M107 P`, `M109 T.. C3 W1`).
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
- Validator: Python `logging` (`ERROR [A250/A350]`, `WARNING [other]`, `INFO [info]`), summary in `finish()`.

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
f0d147d version 02.03.04.06+a250t; 0655774 restore CRLF in `fdm_idex.json`; 0f53633 restore CRLF in `Snapmaker.json`. Details:
`../_snapmaker2-review/integration/snapmaker-profiles.md`.

## Cross-repo interfaces
Full map: `../.claude/knowledge/DEPENDENCIES.md`; facts: `../.claude/knowledge/repos/snapmaker-profiles.json`;
specialist agent: `sm-profiles`.
- **OrcaSlicer** (sm-orca): `.gitmodules` `submodules/Profiles` -> `https://github.com/icaroNZ/snapmaker-profiles.git`
  branch `a250t/main`, gitlink 0655774; `CMakeLists.txt:76-93` + `cmake/SyncSnapmakerProfiles.cmake` (requires
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
