# Eval fixtures

The eval prompts in `../evals.json` reference three fixtures. They are a real 4-layer, 71-part EAGLE 9 /
Fusion Electronics design (a private project) and are **not** shipped in this repository:

| Path | Used by | What it must be |
|---|---|---|
| `gerbers/` | eval 0 | Fusion CAM export of the board: `copper_top_l1.gbr`, `copper_inner_l2.gbr`, `copper_inner_l3.gbr`, `copper_bottom_l4.gbr`, `soldermask_*.gbr`, `silkscreen_*.gbr`, `profile.gbr`, `drill_1_16.xln` (54 x 51 mm outline, smallest drill 0.2 mm, 0.65 mm-pitch parts) |
| `design.sch` | evals 1, 3 | The schematic, with the `SOP65P640X120-29N` and `HRO_TYPE-C-31-M-12` packages in their pre-rework geometry (no solder-mask polygons / plated-slot milling), so `check_consistency.py design.sch design.brd` fails on exactly those two packages |
| `design.brd` | eval 1 | The routed board with the reworked packages (71 parts, 57 nets) |

To run the evals on your own design, drop equivalent files here and adjust the numeric assertions in
`evals.json` (board size, part/net counts, package names). Everything under this directory except this
README is ignored by git.
