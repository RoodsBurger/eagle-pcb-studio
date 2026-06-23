# eagle-pcb-studio

**A Claude Code skill for generating and reviewing Autodesk Fusion Electronics / EAGLE 9.x PCB designs.**

`eagle-pcb-studio` gives an AI coding agent a focused toolset for EAGLE / Fusion Electronics projects — `.sch` schematics, `.brd` boards, `.lbr` libraries, and exported Gerber/drill sets. It does two things:

- **Generate** a placed board from a schematic — read an existing `.sch`, optimize component placement (wire length + area), and emit an unrouted `.brd` ready to route.
- **Review** an existing design and its Gerbers before manufacturing — schematic ERC, board DFM, solder-mask dams, drills, plane pours, trace sizing, schematic↔board consistency, and a fab-ready BOM.

Every script is self-contained and dependency-free (Python 3.8+ standard library; `openpyxl` only for the BOM), so they run inside Claude or standalone from the command line.

## Capabilities

**Generate — turn a schematic into a placed board**

| Script | What it does |
|---|---|
| `scripts/sch_to_board.py` | **Schematic → placed board.** Reads a `.sch`, maps pins→pads via the device connects, auto-sizes the board, runs the HPWL + BLF placer, and emits an unrouted `.brd` — libraries copied verbatim, every net present as a ratsnest. |
| `scripts/find_libraries.py` | Resolve the **`.lbr` footprint libraries** a schematic needs — reports each as embedded, found-on-disk, or missing (searches `components/`, the project tree, EAGLE library roots). |
| `scripts/analyze_schematic.py` | **Schematic ERC/correctness:** floating/single-pin nets, unconnected pins (escalated for input/power), missing values, duplicate refs, power-rail driver sanity, NC-pin checks. |
| `scripts/place_components.py` | Spec-driven **HPWL + bottom-left-fill** placement: minimizes half-perimeter wire length and board area, edge-locks connectors, clusters net groups, pads obstacles so pads never touch. (The engine behind `sch_to_board.py`.) |
| `scripts/render_svg.py` | Render a `.brd` **or a placement** (spec + placements) to SVG for a quick visual check. |

**Review — check a design and its Gerbers before fab**

| Script | What it does |
|---|---|
| `scripts/check_consistency.py` | Schematic↔board footprint + netlist consistency. Diagnoses and (`--sync`) fixes Fusion's *"inconsistent footprints in schematic and board"* ERC error. |
| `scripts/analyze_board.py` | Board DFM: area, placement gaps/overlaps, **route/airwire completeness**, plane-pour verification, IPC-2221 power-trace widths, fine-pitch mask dams, thermal pads/vias. |
| `scripts/analyze_gerbers.py` | Gerber + drill DFM: layer completeness, **solder-mask dam widths** (flags slivers below the fab minimum), drill tools/sizes, routed-slot detection, board size + layer alignment. Handles Fusion naming (`copper_top_l1`, `profile`, `.xln`). |
| `scripts/make_bom.py` | Generate a PCBWay-format assembly BOM `.xlsx` from a CSV (required columns, `DNS` for do-not-populate). |

## Quick start

```bash
# Review a Fusion gerber export before fab
python3 scripts/analyze_gerbers.py "path/to/CAMOutputs" --text

# Turn a schematic into a placed board
python3 scripts/sch_to_board.py design.sch -o design.brd --text

# Fix Fusion's "inconsistent footprints" ERC error (board = source of truth)
python3 scripts/check_consistency.py design.sch design.brd --sync
```

## Install as a Claude Code skill

```bash
git clone https://github.com/RoodsBurger/eagle-pcb-studio.git ~/.claude/skills/eagle-pcb-studio
```

Claude Code discovers it via `SKILL.md`. The scripts also work on their own — no Claude required.

## Workflows

### A — Schematic → placed board
1. `find_libraries.py` — confirm every footprint resolves (embedded or `.lbr` found); gather any missing.
2. `analyze_schematic.py` — catch ERC issues (floating nets, unconnected power pins) before layout.
3. `sch_to_board.py` — resolves each part's footprint, builds a placement spec, runs the HPWL+BLF optimizer, and writes a placed, unrouted `.brd` (auto-sized, or `--board WxH`).
4. Preview with `render_svg.py`, then route in Fusion/EAGLE.

### B — Review before fab
1. `analyze_schematic.py` — schematic ERC/correctness.
2. `check_consistency.py` — clear any "inconsistent footprints" ERC errors.
3. `analyze_board.py` — confirm planes poured, 0 airwires, sane power-trace widths.
4. `analyze_gerbers.py` — layer completeness, drills, solder-mask dams.
5. `make_bom.py` — fab-ready BOM.

## References

In-depth docs the skill loads on demand (`references/`): EAGLE XML format, Gerber/Excellon parsing, placement methodology, the generation pattern, and a manufacturing-prep playbook (solder-mask dams, plated slots, footprint sync, PCBWay/JLCPCB prep).

## Design notes

- **Preserve the DOCTYPE.** EAGLE files begin with `<!DOCTYPE eagle ...>`; edits are spliced into the raw text rather than re-serialized, so files stay Fusion-loadable and diffs stay small.
- **A solder-mask dam can't exceed the copper gap.** On 0.5–0.65 mm-pitch parts, a ≥0.22 mm dam isn't possible without mask-defining the openings or ganging them — the tools account for this rather than over-promising.
- **Fusion Gerbers have no X2 attributes** and use names like `copper_top_l1.gbr` / `profile.gbr` / `drill_1_16.xln`; the analyzer identifies layers by that Fusion naming first, then X2 attributes, then common extensions.

## Requirements

Python 3.8+ (standard library only). `openpyxl` for `make_bom.py`.

## Status / roadmap

Active development. Recently added: schematic ERC (`analyze_schematic.py`), `.lbr` library resolution (`find_libraries.py`), and placement rendering in `render_svg.py`. Possible next: datasheet-aware value checks, autoroute hints, panelization, and more fab presets.

## License

MIT — see [LICENSE](LICENSE). © 2026 Rodolfo Raimundo.
