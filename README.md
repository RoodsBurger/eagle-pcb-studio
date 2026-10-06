# eagle-pcb-studio

**A Claude Agent Skill that generates and reviews EAGLE / Fusion 360 Electronics PCB designs, plus an MCP client that lets Claude drive Fusion 360 itself.**

The skill gives Claude Code deterministic tools for Autodesk Fusion Electronics / EAGLE 9.x files — `.sch` schematics, `.brd` boards, `.lbr` libraries and exported Gerber/drill sets — and the know-how to use them:

- **Generate** a placed board from a schematic: read the `.sch`, resolve its footprint libraries, optimize placement (wire length + area), emit an unrouted `.brd` ready to route.
- **Review** a design and its Gerbers before manufacturing: schematic ERC, board DFM, solder-mask dams, drills, plane pours, trace sizing, schematic↔board consistency, a fab-ready BOM.
- **Drive Fusion 360** over its MCP add-in: upload the pair as a Fusion electronics design, read Fusion's ERC/DRC back, export what Fusion holds, screenshot it, run the 3D interference check.

The analyzers are plain Python over files and need no Fusion; the MCP client is the bridge for the steps that need Fusion open.

## Does the skill help? Measured.

Same boards, same checks, same model (`claude-fable-5-1`), three configurations, graded with scripts where a script can decide (consistency, XML validity, part/net counts, overlaps) and a Claude grader for the rest:

| Configuration | What Claude got | Checks passed | Time per eval | Output tokens |
|---|---|---|---|---|
| **With skill** | the eval prompt + this skill installed | **15/15** | 360 s ± 258 s | 20164 ± 13141 |
| Strong prompt, no skill | the same detailed prompt (it names the parts and the concerns), no skill | **14/15** | 460 s ± 365 s | 29385 ± 23693 |
| No skill | a short, uncoached request for the same task, no skill | **15/15** | 306 s ± 138 s | 23875 ± 10449 |

Three of the four evals ran here (ERC inconsistent footprints, placement, schematic → board; 15 checks). The fourth, the Gerber fab review, needs a Fusion CAM export that only exists on the author's machine, so the solder-mask-dam check — the one the skill won in the earlier run — is not covered yet. On these three evals the uncoached no-skill run matched the skill. The one failed check came from the strong-prompt run: it emitted 32 contactrefs to pads the schematic's package does not define. The with-skill run first did the same (the skill's own generator had that bug; the eval caught it, `sch_to_board.py` was fixed, and the table shows the rerun — the first run's grading is kept in `archive/`). One run per configuration, so treat differences of one check as noise.

Full numbers, per-eval breakdown and every failed check with its evidence: [`evals/results/2026-10-06/benchmark.md`](evals/results/2026-10-06/benchmark.md). Eval definitions and the three prompt variants: [`evals/evals.json`](evals/evals.json). The fixtures are a private 4-layer, 71-part board and are not shipped; [`evals/fixtures/README.md`](evals/fixtures/README.md) says what to drop in to rerun.

An earlier run of the same eval set (June 2026, skill-creator harness, 1 run per configuration) scored **20/20 with the skill vs 19/20 with the strong prompt**; its one miss was the strong-prompt run calling the USB-C solder mask clean and missing 0.048 mm dams. Its raw workspace is not in this repository, and its `benchmark.md` rendered "0% ± 0%" because the skill-creator aggregator defaulted a missing `pass_rate` to zero and printed ±0 for single runs — the aggregator here fixes both.

## Setup

**1. Install the skill** — clone it where Claude Code reads skills from:

```bash
# personal: available in every project
git clone https://github.com/RoodsBurger/eagle-pcb-studio.git ~/.claude/skills/eagle-pcb-studio
# or per project
git clone https://github.com/RoodsBurger/eagle-pcb-studio.git .claude/skills/eagle-pcb-studio
```

Claude Code discovers it through `SKILL.md` and uses it when a request matches. Requirements: Python 3.8+ standard library; `pip install openpyxl` only for `make_bom.py`.

**2. (Optional) Connect Fusion 360** — start Fusion with its MCP add-in (Utilities → Add-Ins), then:

```bash
python3 scripts/fusion_mcp.py tools                 # lists the add-in's tools when it is reachable
python3 scripts/fusion_mcp.py find-port             # if it is not on the default port 52435 …
export FUSION_MCP_URL=http://127.0.0.1:<port>/mcp   # … point the client at it
```

**3. (Optional) Run the evals yourself**

```bash
# drop fixtures into evals/fixtures/ (see its README), then:
python3 evals/run_eval.py --config with_skill strong_prompt no_skill --eval 1 2 3 --parallel 3
python3 evals/grade_run.py --all /tmp/eagle-pcb-studio-workspace/<date>
python3 evals/aggregate_benchmark.py /tmp/eagle-pcb-studio-workspace/<date>
```

The runner uses the Claude Code CLI headlessly with an isolated config (your login only — no personal skills, plugins or memory), one directory per run in a workspace **outside** this repository (a run explores its parent directories, and a no-skill run that finds `SKILL.md` is no baseline — the runner refuses an in-repo workspace), and the same fixtures for every configuration. Tests for the MCP client run against a mock server: `python3 -m unittest tests/test_fusion_mcp.py`.

## Using it

Ask Claude in plain language; it picks the tool and runs it:

- *"Check my Fusion gerber export in `./CAMOutputs` before I send it to the fab."*
- *"Turn my schematic `design.sch` into a placed board."*
- *"Fusion says my board and schematic have inconsistent footprints — fix it."*
- *"Place these parts on a 30×30 mm board to minimize area and keep traces short."*
- *"Upload the pair to Fusion and tell me what its DRC says."*
- *"Make a PCBWay assembly BOM from `parts.csv`."*

## How Claude drives Fusion 360

`scripts/fusion_mcp.py` is a streamable-HTTP JSON-RPC client for the **Fusion MCP add-in**, the MCP server that runs inside Fusion 360 and exposes its Python API. The add-in offers three tools — `fusion_mcp_read` (open document, screenshot, active command), `fusion_mcp_execute` (run Fusion API Python, open a document) and `fusion_mcp_electronics_read` (parts, nets, elements, signals, layers, ERC/DRC errors) — and the client wraps them into the steps a PCB loop needs:

```bash
F="python3 scripts/fusion_mcp.py"
$F upload-pair out/design.sch out/design.brd --folder <folder-id>    # one electronics design (uploadAssembly)
$F folder <folder-id>                                                # the new fsch / fbrd ids
$F read-design --sch <fsch-id> --brd <fbrd-id> --png board.png       # parts, nets, elements, signals, layers, ERC+DRC
$F export <fbrd-id> fusion-export.brd                                # EAGLE XML of what Fusion holds
python3 scripts/check_consistency.py out/design.sch fusion-export.brd
$F interference <f3d-id>                                             # collisions on a pushed 3D PCB
$F script my_fusion_script.py --read-only                            # any Fusion API Python (run(context))
```

Claude follows **Workflow C** in `SKILL.md`: upload, read back, compare with the local files, export for the local analyzers, 3D-check — and `references/fusion-mcp.md` carries the tool arguments and the Fusion-side gotchas (uploads only progress while the event loop is pumped, every upload is a new lineage, never re-upload an export, scripts finish even if the client dies). When Fusion is not running, the client says so and Claude stays file-based.

## What's inside

Dependency-free tools (Python standard library; `openpyxl` for the BOM) that Claude runs as needed.

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

**Fusion 360 bridge**

| Tool | What it does |
|---|---|
| `fusion_mcp.py` | MCP client for the Fusion add-in: `tools`, `script`, `open`, `folder`, `upload`, `upload-pair`, `electronics`, `read-design`, `export`, `screenshot`, `interference`, `wait-idle`, `find-port`. Library and CLI. |

**Evals** (`evals/`): `evals.json` (4 tasks × 5 assertions, three prompt variants), `run_eval.py` (headless runner), `grade_run.py` (script checks + grader model), `aggregate_benchmark.py` (benchmark.json / benchmark.md), `results/` (saved runs).

## References

In-depth docs the skill loads on demand (`references/`): EAGLE XML format, Gerber/Excellon parsing, placement methodology, the generation pattern, a manufacturing-prep playbook (solder-mask dams, plated slots, footprint sync, PCBWay/JLCPCB prep), and driving Fusion 360 over MCP.

## Design notes

- **Preserve the DOCTYPE.** EAGLE files begin with `<!DOCTYPE eagle ...>`; edits are spliced into the raw text rather than re-serialized, so files stay Fusion-loadable and diffs stay small.
- **A solder-mask dam can't exceed the copper gap.** On 0.5–0.65 mm-pitch parts, a ≥0.22 mm dam isn't possible without mask-defining the openings or ganging them — the tools account for this rather than over-promising.
- **Fusion Gerbers have no X2 attributes** and use names like `copper_top_l1.gbr` / `profile.gbr` / `drill_1_16.xln`; the analyzer identifies layers by that Fusion naming first, then X2 attributes, then common extensions.
- **The skill is file-based; MCP is the Fusion bridge.** Nothing in the analyzers depends on MCP. `fusion_mcp.py` is the only component that speaks MCP, and only to Fusion.

## License

MIT — see [LICENSE](LICENSE). © 2026 Rodolfo Raimundo.
