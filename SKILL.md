---
name: eagle-pcb-studio
description: >-
  Generate and review Autodesk Fusion Electronics / EAGLE 9.x PCB designs —
  schematics (.sch), boards (.brd), vendor libraries (.lbr), and Fusion-exported
  Gerber/drill sets. Use this skill WHENEVER the user works with EAGLE or Fusion
  Electronics PCB files or Fusion Gerbers (copper_top_l1.gbr, soldermask_top.gbr,
  profile.gbr, drill_*.xln), OR asks to: turn a schematic into a placed board
  (auto-place, minimize area / wire length); find or resolve the .lbr footprint
  libraries a schematic needs; check a schematic for ERC/correctness (floating nets,
  unconnected or power pins, missing values); run DRC/DFM, "check my board", or "is
  this ready to fab/order"; fix schematic↔board "inconsistent footprints" ERC errors;
  measure or fix thin solder-mask dams/slivers; convert holes to plated slots; verify
  ground/power planes are poured; size power traces (IPC-2221); render a board or a
  placement to SVG; or build a PCBWay/JLCPCB assembly BOM. This is the EAGLE/Fusion
  counterpart to the kicad-happy plugin — reach for it for ANY EAGLE/Fusion
  hardware-design question even if the skill isn't named. Do NOT use it for KiCad
  projects (.kicad_pcb/.kicad_sch — use kicad-happy), Altium or other non-EAGLE EDA
  tools, general circuit-theory or firmware questions, spreadsheet or image tasks, or
  pure component sourcing.
---

# EAGLE / Fusion Electronics — PCB Studio

This skill does two jobs for **EAGLE 9.x / Autodesk Fusion Electronics** designs:

1. **Generate** — turn an existing schematic (`.sch`) into a **placed board** (`.brd`) with optimized component placement, resolving the footprint libraries it needs and previewing the result.
2. **Review** — check a design and its Gerber export for manufacturing: schematic ERC, board DFM, solder-mask dams, plane pours, trace sizing, sch↔board consistency, and a fab-ready BOM.

> **Scope check.** EAGLE/Fusion store designs as XML (`.brd`/`.sch` with a `<!DOCTYPE eagle ...>`) — *not* KiCad's `.kicad_pcb`. If the user has a KiCad project, use **kicad-happy**. If they have `.brd`/`.sch`/`.lbr` or Fusion-named Gerbers, this is the right skill.

All scripts are self-contained, stdlib-only (Python 3.8+; `make_bom.py` also needs `openpyxl`), take file paths as arguments, and print a `--text` summary or JSON (`-o`). Real-project paths often contain spaces — **always quote them.**

## The one rule that prevents the most damage

EAGLE/Fusion files begin with `<?xml ...?>` and `<!DOCTYPE eagle SYSTEM "eagle.dtd">`. **Never** rewrite a `.brd`/`.sch`/`.lbr` by parsing it with ElementTree and re-serializing the whole tree — that drops the DOCTYPE and reorders everything, which can break Fusion loading and explodes the diff. **Parse for analysis, but apply edits by splicing the changed `<package>`/`<element>`/etc. block back into the raw file text**, leaving the prologue and everything else byte-identical. Every script here follows this; do the same in any ad-hoc edit.

## Scripts (the deterministic engines)

Run these directly; don't reimplement what they already do. Quote `<...>` paths.

| Script | Command | Use it to |
|---|---|---|
| `sch_to_board.py` | `python3 scripts/sch_to_board.py "<in.sch>" -o "<out.brd>" [--board WxH]` | **Schematic → placed board.** Reads a `.sch`, maps pins→pads, auto-sizes the board, runs the HPWL+BLF placer, emits an unrouted `.brd` (libraries copied verbatim, every net a ratsnest). |
| `find_libraries.py` | `python3 scripts/find_libraries.py "<sch>" [--search DIR]...` | Resolve the **footprint `.lbr` libraries** a schematic needs — reports each as embedded, found-on-disk (searches `components/`, the project tree, EAGLE library roots), or MISSING. Run before `sch_to_board.py`. |
| `analyze_schematic.py` | `python3 scripts/analyze_schematic.py "<sch>" [--text]` | **Schematic ERC/correctness:** floating/single-pin nets, unconnected pins (escalated for input/power), missing values, duplicate refs, power-rail driver/supply sanity, NC-pin checks. |
| `place_components.py` | `python3 scripts/place_components.py "<spec.json>" -o out.json` (or `--demo`) | Spec-driven **HPWL + bottom-left-fill** placement: minimizes wire length + area, edge-locks connectors, clusters net groups, pads obstacles so pads never touch. (Engine behind `sch_to_board.py`; use directly to place a from-scratch spec.) |
| `analyze_gerbers.py` | `python3 scripts/analyze_gerbers.py "<export-dir>" [--mask-threshold 0.22]` | Gerber+drill DFM: layer completeness, **solder-mask dam widths**, drill sizes, routed-slot detection, board size + alignment. Handles Fusion naming. |
| `analyze_board.py` | `python3 scripts/analyze_board.py "<board.brd>"` | Board DFM: area, placement gaps/overlaps, **airwire/route completeness**, plane-pour verification, IPC-2221 power-trace widths, fine-pitch mask dams, thermal pads/vias. |
| `check_consistency.py` | `python3 scripts/check_consistency.py "<sch>" "<brd>" [--sync]` | Verify sch↔board footprints are identical (source of the **"inconsistent footprints"** ERC error) + netlist diff. `--sync` repairs the `.sch` from the `.brd`. |
| `render_svg.py` | `python3 scripts/render_svg.py "<board.brd>" -o out.svg` — or `--spec spec.json --placements pl.json` | Render a **`.brd`** or a **placement** (spec + placements) to SVG for a fast visual check. |
| `make_bom.py` | `python3 scripts/make_bom.py "<bom.csv>" -o PCBWay-BOM.xlsx` | Convert a BOM CSV into a **PCBWay-format assembly `.xlsx`** (`DNS` for do-not-populate, LCSC#). |

## References (read on demand)

| File | Read when |
|---|---|
| `references/eagle-xml-format.md` | Editing/emitting `.brd`/`.sch`/`.lbr` XML — layer numbers, libraries/packages, devices, signals/planes, designrules, the DOCTYPE/splice rule. |
| `references/gerber-parsing.md` | Parsing RS-274X / Excellon, or extending the Gerber analyzer — apertures, G36 regions, Fusion vs KiCad naming, dam-width recipe. |
| `references/placement-methodology.md` | Tuning placement — HPWL, BLF area minimization, group clustering, obstacle padding, edge locking. |
| `references/generation-pattern.md` | Writing a board from a code spec (no `.sch`) — PKG/DEVSETS/PARTS/nets, `parse_vendor()` verbatim `.lbr` footprints, keeping sch/brd byte-identical. |
| `references/manufacturing-prep.md` | Prepping for fab — solder-mask dam fixes (mask-defined vs expose/gang), plated-slot idiom, footprint sync, PCBWay/JLCPCB BOM + order params. |

## Workflow A — Schematic → placed board

Use when the user has a schematic and wants a board, or says "make a board from this", "place these parts", "minimize the area".

1. **Resolve libraries** — `find_libraries.py "<sch>"`. Confirm every footprint is embedded or its `.lbr` is found; gather any MISSING before proceeding.
2. **ERC the schematic** — `analyze_schematic.py "<sch>"` to catch floating nets / unconnected power pins *before* laying out.
3. **Generate the placed board** — `sch_to_board.py "<sch>" -o "<brd>"` (HPWL+BLF placement, auto-sized). Pass `--board WxH` to fix the outline.
4. **Preview** — `render_svg.py "<brd>" -o board.svg`, iterate, then route in Fusion/EAGLE.

*(No schematic yet? Build a `place_components` spec and emit the board from code — see `references/generation-pattern.md`.)*

## Workflow B — Review an existing design before fab

Use for "check my board", "is this ready to order", "review before fab", DFM, or after a fab flags an issue.

1. **Schematic** — `analyze_schematic.py "<sch>"` for ERC/correctness.
2. **Consistency** — `check_consistency.py "<sch>" "<brd>"`; if it flags "inconsistent footprints", `--sync` repairs it. (The benign "POWER pin connected to net" ERC *warnings* are just acknowledgements — approve them.)
3. **Board DFM** — `analyze_board.py "<brd>"`: planes poured, 0 airwires, sane power-trace widths, fine-pitch dams.
4. **Gerber DFM** — `analyze_gerbers.py "<export-dir>"`: completeness, drills, solder-mask dams. Thin dams on ≤0.65mm-pitch parts are expected — see below.
5. **Fix** per `references/manufacturing-prep.md`, then **re-run** the analyzers.
6. **BOM** — `make_bom.py "<csv>" -o PCBWay-BOM.xlsx`.
7. **Order params live in the fab's web form, not the Gerbers**: layer count, thickness, copper weight, **surface finish (ENIG for fine pitch)**, mask/silk color, single-board vs panel. Say so explicitly.

## Gotchas worth internalizing (the *why*)

- **Fusion Gerbers have no X2 attributes** and use names like `copper_top_l1.gbr` / `profile.gbr` / `drill_1_16.xln`. KiCad-oriented tools mis-flag them as "missing layers." `analyze_gerbers.py` handles the Fusion naming first.
- **A solder-mask dam can't exceed the copper gap.** On a 0.5–0.65mm-pitch part the pads may be ~0.2mm apart, so a ≥0.22mm dam is impossible without the mask encroaching onto the pads (mask-defined) or you *expose/gang* the area (standard for fine pitch). Don't promise a wider dam than the geometry allows.
- **Plated slots, the EAGLE way:** a round pad **plus a single `layer-46` (Milling) wire through it, width = drill diameter** — *not* a closed milling outline (which exports as a round drill **plus** a separate cutout that fabs flag as "slot and hole overlapped"). Native Excellon can't emit slots, so a default CAM job may still leave the round drill — tell the fab "round drill + route = same plated slot."
- **Footprint geometry flows `.lbr` → generator → `.sch`/`.brd`.** To change a footprint, edit the `.lbr` and regenerate; the routed `.sch`/`.brd` embed copies that must stay byte-identical or ERC complains. A saved `.sch` already embeds its libraries — `find_libraries.py` reports embedded vs needs-the-`.lbr`.
- **Planes "not pouring" in the Fusion editor** is usually a display/ratsnest quirk — confirm against the actual Gerber (`analyze_gerbers.py` poured-region count), not the on-screen preview.
