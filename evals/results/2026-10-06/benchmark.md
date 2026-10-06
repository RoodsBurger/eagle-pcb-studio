# Skill benchmark: eagle-pcb-studio

**Executor model**: claude-fable-5-1  
**Grader**: scripts (check_consistency.py, analyze_board.py, XML parsing) for mechanical checks; Claude grader model for the rest  
**Date**: 2026-10-06T17:54:09Z  
**Evals**: 1 erc-inconsistent-footprints, 2 placement-min-area, 3 sch-to-board — 1 run(s) per configuration  

## Configurations

- **with skill** — the eval prompt; the skill installed in the run's .claude/skills/ and named in the prompt (read SKILL.md, follow it)
- **strong prompt** — the same detailed eval prompt (it names the parts and the concerns), no skill
- **no skill** — the eval's plain_prompt: a short, uncoached request for the same task, no skill (prompts in evals.json)

## Summary

| Metric | with skill | strong prompt | no skill |
|---|---|---|---|
| Checks passed | **15/15** | **14/15** | **15/15** |
| Pass rate | 100% ± 0% | 93% ± 12% | 100% ± 0% |
| Time per eval | 360 s ± 258 s | 460 s ± 365 s | 306 s ± 138 s |
| Output tokens per eval | 20164 ± 13141 | 29385 ± 23693 | 23875 ± 10449 |
| Cost per eval (USD, list price) | 2.38 ± 1.12 | 3.24 ± 2.59 | 2.33 ± 1.04 |

Deltas (first configuration minus the others): with_skill - strong_prompt: pass rate +0.07, time -99.6 s, tokens -9221; with_skill - no_skill: pass rate +0.00, time +54.2 s, tokens -3711

## Per eval

| Eval | with skill | strong prompt | no skill |
|---|---|---|---|
| 1 erc-inconsistent-footprints | 5/5 (183 s) | 5/5 (250 s) | 5/5 (187 s) |
| 2 placement-min-area | 5/5 (241 s) | 5/5 (248 s) | 5/5 (274 s) |
| 3 sch-to-board | 5/5 (657 s) | 4/5 (881 s) | 5/5 (457 s) |

## Failed checks

- **eval 3 / strong prompt** — Every schematic net appears as a <signal> with the correct contactrefs (unrouted ratsnest).  
  57 signals for 57 schematic nets (missing: none); contactrefs 284 on the board vs 252 implied by the schematic's device connects (pads that exist in the package); missing: none; unexpected or dangling (pad absent from the package): GND:J_USB.S1B, GND:J_USB.S1C, GND:J_USB.S1D, GND:J_USB.S1E, GND:J_USB.S1F, GND:J_USB.S1G … (+26)

## Notes

- Executor and grader model: claude-fable-5-1 via the Claude Code CLI, isolated config dir, skills and MCP servers disabled, one run per configuration and eval.
- Eval 0 (gerber-fab-review) was not run: its fixture is a Fusion CAM export that exists only on the author's personal machine. Nothing here covers the solder-mask-dam check where the June 2026 run saw the skill's one win (0.048 mm dams).
- Eval 3 with_skill: the first run scored 4/5. sch_to_board.py emitted 32 contactrefs to USB-C shield pads that the schematic's embedded package does not define (the device connects name them; the package copy is out of sync). Fixed in sch_to_board.py (pads absent from the package are dropped with a WARNING); the table shows the rerun with the fixed skill. The first run's grading is kept under archive/.
- Eval 3 strong_prompt: 4/5 for the same reason (284 contactrefs incl. 32 dangling). The no_skill run connected only pads that exist (252) and passed.
- Grading: assertions a script can decide (XML validity, DOCTYPE, check_consistency, part/net counts, exact contactref sets against the device connects restricted to existing pads, overlaps, bounds, edge placement, SVG validity) are decided by scripts; the rest (what the transcript identified and explained) by the grader model with quoted evidence.
- Earlier run (June 2026, skill-creator harness, 1 run per configuration): with skill 20/20, strong prompt 19/20; raw workspace not in this repository. Its benchmark.md showed '0% ± 0%' because the skill-creator aggregator defaults a missing summary.pass_rate to 0.0 and prints ±0 for single runs; this aggregator derives pass_rate from passed/total and prints n=1.
