# eagle-pcb-studio

A **Claude Agent Skill** for generating and reviewing Autodesk Fusion Electronics / EAGLE 9.x PCB designs — `.sch` schematics, `.brd` boards, `.lbr` libraries, and exported Gerber/drill sets.

It does two things:

- **Generate** a placed board from a schematic — read an existing `.sch`, optimize component placement (wire length + area), and emit an unrouted `.brd` ready to route.
- **Review** an existing design and its Gerbers before manufacturing — schematic ERC, board DFM, solder-mask dams, drills, plane pours, trace sizing, schematic↔board consistency, and a fab-ready BOM.

## Install

It's a skill — clone it into the folder Claude Code reads skills from:

```bash
# Personal — available across all your projects
git clone https://github.com/RoodsBurger/eagle-pcb-studio.git ~/.claude/skills/eagle-pcb-studio

# …or per-project — committed alongside one repo
git clone https://github.com/RoodsBurger/eagle-pcb-studio.git .claude/skills/eagle-pcb-studio
```

Claude Code discovers it automatically through `SKILL.md` and uses it when your request matches — nothing else to configure.

## Using it

Just ask Claude in plain language; it picks the right tool and runs it for you:

- *"Check my Fusion gerber export in `./CAMOutputs` before I send it to the fab."*
- *"Turn my schematic `design.sch` into a placed board."*
- *"Fusion says my board and schematic have inconsistent footprints — fix it."*
- *"Place these parts on a 30×30 mm board to minimize area and keep traces short."*
- *"Make a PCBWay assembly BOM from `parts.csv`."*

## What's inside

The skill bundles focused, dependency-free tools (Python 3.8+ standard library; `openpyxl` for the BOM) that Claude runs as needed.

**Generate — schematic → placed board**

| Tool | What it does |
|---|---|
| `sch_to_board.py` | **Schematic → placed board.** Reads a `.sch`, maps pins→pads via the device connects, auto-sizes the board, runs the HPWL + BLF placer, and emits an unrouted `.brd` — libraries copied verbatim, every net present as a ratsnest. |
| `find_libraries.py` | Resolve the **`.lbr` footprint libraries** a schematic needs — reports each as embedded, found-on-disk, or missing (searches `components/`, the project tree, EAGLE library roots). |
| `analyze_schematic.py` | **Schematic ERC/correctness:** floating/single-pin nets, unconnected pins (escalated for input/power), missing values, duplicate refs, power-rail driver sanity, NC-pin checks. |
| `place_components.py` | Spec-driven **HPWL + bottom-left-fill** placement: minimizes half-perimeter wire length and board area, edge-locks connectors, clusters net groups, pads obstacles so pads never touch. (The engine behind `sch_to_board.py`.) |
| `render_svg.py` | Render a `.brd` **or a placement** (spec + placements) to SVG for a quick visual check. |

**Review — check a design and its Gerbers before fab**

| Tool | What it does |
|---|---|
| `check_consistency.py` | Schematic↔board footprint + netlist consistency. Diagnoses and (`--sync`) fixes Fusion's *"inconsistent footprints in schematic and board"* ERC error. |
| `analyze_board.py` | Board DFM: area, placement gaps/overlaps, **route/airwire completeness**, plane-pour verification, IPC-2221 power-trace widths, fine-pitch mask dams, thermal pads/vias. |
| `analyze_gerbers.py` | Gerber + drill DFM: layer completeness, **solder-mask dam widths** (flags slivers below the fab minimum), drill tools/sizes, routed-slot detection, board size + layer alignment. Handles Fusion layer naming (`copper_top_l1`, `profile`, `.xln`). |
| `make_bom.py` | Generate a PCBWay-format assembly BOM `.xlsx` from a CSV (required columns, `DNS` for do-not-populate). |

## How it works

**Generate a board** — Claude resolves the footprint libraries (`find_libraries.py`), runs schematic ERC (`analyze_schematic.py`), generates the placed board with the HPWL + BLF optimizer (`sch_to_board.py`, auto-sized or a fixed `--board WxH`), and previews it (`render_svg.py`).

**Review before fab** — Claude runs schematic ERC, clears any "inconsistent footprints" errors (`check_consistency.py --sync`), checks the board (planes poured, 0 airwires, sane power-trace widths via `analyze_board.py`), checks the Gerbers (completeness, drills, solder-mask dams via `analyze_gerbers.py`), and produces a fab-ready BOM (`make_bom.py`). Fab build settings — layer count, thickness, surface finish (ENIG for fine pitch), colors — are set in the fab's order form, not the Gerbers.

## References

In-depth docs the skill loads on demand (`references/`): EAGLE XML format, Gerber/Excellon parsing, placement methodology, the generation pattern, and a manufacturing-prep playbook (solder-mask dams, plated slots, footprint sync, PCBWay/JLCPCB prep).

## Design notes

- **Preserve the DOCTYPE.** EAGLE files begin with `<!DOCTYPE eagle ...>`; edits are spliced into the raw text rather than re-serialized, so files stay Fusion-loadable and diffs stay small.
- **A solder-mask dam can't exceed the copper gap.** On 0.5–0.65 mm-pitch parts, a ≥0.22 mm dam isn't possible without mask-defining the openings or ganging them — the tools account for this rather than over-promising.
- **Fusion Gerbers have no X2 attributes** and use names like `copper_top_l1.gbr` / `profile.gbr` / `drill_1_16.xln`; the analyzer identifies layers by that Fusion naming first, then X2 attributes, then common extensions.

## Requirements

The skill's tools use Python 3.8+ (standard library only); `make_bom.py` also uses `openpyxl`.

## Status

Active development. Recently added: schematic ERC (`analyze_schematic.py`), `.lbr` library resolution (`find_libraries.py`), and placement rendering in `render_svg.py`. Possible next: datasheet-aware value checks, autoroute hints, panelization, and more fab presets.

## License

MIT — see [LICENSE](LICENSE). © 2026 Rodolfo Raimundo.
