---
name: eagle-pcb-studio
description: >-
  Generate and review Autodesk Fusion Electronics / EAGLE 9.x PCB designs —
  schematics (.sch), boards (.brd), vendor libraries (.lbr), and Fusion-exported
  Gerber/drill sets. Use this skill WHENEVER the user works with EAGLE or Fusion
  Electronics PCB files or Fusion Gerbers (copper_top_l1.gbr, soldermask_top.gbr,
  profile.gbr, drill_*.xln), OR asks to: generate/create a schematic or board from a
  parts+netlist spec; place components, minimize board area, or optimize for wire
  length; render a board to SVG; run DRC/DFM, "check my board", or "is this ready to
  fab/order"; fix schematic↔board "inconsistent footprints" ERC errors; measure or fix
  thin solder-mask dams/slivers; convert holes to plated slots; verify ground/power
  planes are poured; size power traces (IPC-2221); or build a PCBWay/JLCPCB assembly
  BOM. This is the EAGLE/Fusion counterpart to the kicad-happy plugin — reach for it
  for ANY EAGLE/Fusion hardware-design question even if the skill isn't named. For
  KiCad-native .kicad_pcb/.kicad_sch projects, use kicad-happy instead.
---

# EAGLE / Fusion Electronics — PCB Studio

This skill does two jobs for **EAGLE 9.x / Autodesk Fusion Electronics** designs:

1. **Generate** a schematic (`.sch`) + board (`.brd`) from a Python parts/netlist spec, with optimized component placement and an SVG preview.
2. **Review** an existing design and its Gerber export for manufacturing — DRC/DFM, solder-mask dams, plane pours, trace sizing, sch↔board consistency, and a fab-ready BOM.

> **Scope check.** EAGLE/Fusion store designs as XML (`.brd`/`.sch` with a `<!DOCTYPE eagle ...>`) — *not* KiCad's `.kicad_pcb`. If the user has a KiCad project, use **kicad-happy** instead. If they have `.brd`/`.sch`/`.lbr` or Fusion-named Gerbers, this is the right skill.

All scripts are self-contained, stdlib-only (Python 3.8+; `make_bom.py` also needs `openpyxl`), take file paths as arguments, and print a `--text` summary or JSON (`-o`). Paths in real projects often contain spaces — **always quote them.**

---

## The one rule that prevents the most damage

EAGLE/Fusion files begin with `<?xml ...?>` and `<!DOCTYPE eagle SYSTEM "eagle.dtd">`. **Never** rewrite a `.brd`/`.sch`/`.lbr` by parsing it with ElementTree and re-serializing the whole tree — that drops the DOCTYPE and reorders/reformats everything, which can break Fusion loading and explodes your diff. **Parse for analysis, but apply edits by splicing the changed `<package>`/`<element>`/etc. block back into the raw file text**, leaving the prologue and everything else byte-identical. Every script here already follows this; do the same in any ad-hoc edit.

---

## Scripts (the deterministic engines)

Run these directly; don't reimplement what they already do. `<...>` paths must be quoted.

| Script | Command | Use it to |
|---|---|---|
| `analyze_gerbers.py` | `python3 scripts/analyze_gerbers.py "<export-dir>" [--text] [--mask-threshold 0.22]` | Review a Fusion/EAGLE Gerber+drill export: layer completeness, **solder-mask dam widths** (flags slivers below fab min), drill tools/sizes, routed-slot detection, board size + layer alignment. Recognizes Fusion names (`copper_top_l1`, `profile`, `.xln`) that lack X2 attributes. |
| `analyze_board.py` | `python3 scripts/analyze_board.py "<board.brd>" [--text]` | Board-level DFM: area, placement gaps/overlaps, **airwire/route completeness**, plane-pour verification, net-class/power-trace widths (IPC-2221), fine-pitch mask-dam vs pitch, thermal-pad/via checks. |
| `check_consistency.py` | `python3 scripts/check_consistency.py "<sch>" "<brd>" [--sync]` | Verify schematic↔board footprints are identical (the source of Fusion ERC **"inconsistent footprints in schematic and board"**) + netlist (parts/nets) diff. `--sync` repairs the `.sch` from the `.brd` (board = truth), backing up first. |
| `place_components.py` | `python3 scripts/place_components.py "<spec.json>" [--text] -o out.json` (or `--demo`) | Spec-driven **HPWL + bottom-left-fill** placement: minimizes half-perimeter wirelength, packs to minimize area, keeps connectors on the edge, clusters group members, pads obstacles so pads never touch. |
| `render_svg.py` | `python3 scripts/render_svg.py "<board.brd>" -o out.svg` | Render a `.brd` to SVG for a fast visual sanity check (outline, copper, pads/vias, silk). |
| `make_bom.py` | `python3 scripts/make_bom.py "<bom.csv>" -o PCBWay-BOM.xlsx` | Convert an authoritative BOM CSV into a **PCBWay-format assembly `.xlsx`** (required columns, `DNS` for do-not-populate, LCSC#). |

---

## References (read on demand)

Load the relevant file when you need depth — don't paste them wholesale into context.

| File | Read when |
|---|---|
| `references/eagle-xml-format.md` | Editing/emitting `.brd`/`.sch`/`.lbr` XML — layer numbers, libraries/packages, devices, signals/planes, designrules, the DOCTYPE/splice rule. |
| `references/gerber-parsing.md` | Parsing RS-274X / Excellon by hand, or extending the Gerber analyzer — format spec, apertures, G36 regions, Fusion vs KiCad naming, dam-width recipe. |
| `references/placement-methodology.md` | Tuning or understanding placement — HPWL, BLF area minimization, group clustering, obstacle padding, edge locking. |
| `references/generation-pattern.md` | Writing a project's `generate_sch.py` + `generate_brd.py` — the PKG/DEVSETS/PARTS/nets spec, `parse_vendor()` (verbatim `.lbr` footprints), keeping sch/brd byte-identical. |
| `references/manufacturing-prep.md` | Prepping for fab — solder-mask dam fixes (mask-defined vs expose/gang), plated-slot idiom, footprint sync, PCBWay/JLCPCB BOM + order params. |

---

## Workflow A — Generate a new design

Use when the user wants a board created from a parts list + connections.

1. **Capture the spec.** Components (with packages), the netlist (which pins connect), any vendor parts (real footprints from `.lbr` files in a `components/` dir), board size, and constraints (edge connectors, mounting holes, single- vs double-sided).
2. **Read `references/generation-pattern.md`** and write a `generate_sch.py` (emits the schematic) and `generate_brd.py` (emits the board). Pull vendor footprints **verbatim** from their `.lbr` via a `parse_vendor()` step — re-deriving footprints by hand causes ERC consistency errors.
3. **Place** with `place_components.py` (HPWL + BLF) — see `references/placement-methodology.md` to tune weights, groups, and edge locks.
4. **Preview** with `render_svg.py` and iterate on placement/area.
5. **Keep sch and brd footprints byte-identical** (see the rule above) — verify with `check_consistency.py`.

## Workflow B — Review an existing design before fab

Use for "check my board", "is this ready to order", "review before fab", DFM, or after a fab flags an issue.

1. **Consistency** — `check_consistency.py "<sch>" "<brd>"`. If it flags "inconsistent footprints", that's the exact ERC error Fusion shows; `--sync` repairs it. (The benign "POWER pin connected to net" ERC *warnings* are just acknowledgements — approve them.)
2. **Board DFM** — `analyze_board.py "<brd>"`: confirm planes are actually poured, 0 airwires, sane trace widths for power, and fine-pitch mask dams.
3. **Gerber DFM** — `analyze_gerbers.py "<export-dir>"`: layer completeness, smallest drill, and **solder-mask dam widths**. Thin dams on fine-pitch parts (≤0.65mm pitch) are expected — see below.
4. **Fix** per `references/manufacturing-prep.md`, then **re-run** the analyzers.
5. **BOM** — `make_bom.py "<csv>" -o PCBWay-BOM.xlsx`.
6. **Order params live in the fab's web form, not the Gerbers**: layer count, thickness, copper weight, **surface finish (ENIG for fine pitch)**, mask/silk color, single-board vs panel. Say so explicitly.

---

## Gotchas worth internalizing (the *why*)

- **Fusion Gerbers have no X2 attributes** and use names like `copper_top_l1.gbr` / `profile.gbr` / `drill_1_16.xln`. Generic (KiCad-oriented) tools mis-flag them as "missing layers." `analyze_gerbers.py` handles the Fusion naming first.
- **A solder-mask dam can't exceed the copper gap.** On a 0.5–0.65mm-pitch part the copper pads may be ~0.2mm apart, so a ≥0.22mm dam is impossible without the mask encroaching onto the pads (mask-defined), or you *expose/gang* the area (standard for fine pitch). Don't promise a wider dam than the geometry allows.
- **Plated slots, the EAGLE way:** a round pad **plus a single `layer-46` (Milling) wire through it, width = drill diameter** — *not* a closed milling outline around the pad. A closed outline exports as a round drill **plus** a separate cutout that fabs flag as "slot and hole overlapped." Native Excellon can't emit slots, so a default CAM job may still leave the round drill — tell the fab "round drill + route = same plated slot."
- **Footprint geometry flows `.lbr` → generator → `.sch`/`.brd`.** To change a footprint, edit the `.lbr` and regenerate; the routed `.sch`/`.brd` embed copies that must stay byte-identical or ERC complains.
- **Planes "not pouring" in the Fusion editor** is usually a display/ratsnest quirk — confirm against the actual Gerber (`analyze_gerbers.py` / the poured region count), not the on-screen preview.
