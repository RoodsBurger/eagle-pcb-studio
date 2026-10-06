#!/usr/bin/env python3
"""Aggregate grading.json files into benchmark.json + benchmark.md for any number of configurations.

Layout read:  <workspace>/eval-<id>/<config>/run-<n>/grading.json  (+ timing.json, eval_metadata.json)

Differences from the skill-creator aggregator this replaces:
- pass_rate is derived from passed/total when a grading.json lacks summary.pass_rate (the skill-creator
  script defaulted it to 0.0, which rendered a 100 % result as "0% ± 0%");
- a configuration with a single run prints "n=1" instead of a meaningless "± 0";
- every configuration found is rendered (not just the first two), with deltas against the first one;
- the per-eval breakdown and total checks (e.g. 15/15) are part of the markdown;
- it refuses to write a benchmark when no grading.json was found at all.
"""
import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone

ORDER = ["with_skill", "strong_prompt", "no_skill", "without_skill", "old_skill", "new_skill"]


def stats(values):
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    n = len(values)
    mean = sum(values) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    return {"mean": round(mean, 4), "stddev": round(sd, 4), "min": round(min(values), 4), "max": round(max(values), 4), "n": n}


def load_runs(ws):
    runs, evals = [], {}
    for eval_dir in sorted(glob.glob(os.path.join(ws, "eval-*"))):
        meta_p = os.path.join(eval_dir, "eval_metadata.json")
        meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
        eid = meta.get("eval_id", int(eval_dir.rsplit("-", 1)[1]))
        evals[eid] = meta.get("eval_name", os.path.basename(eval_dir))
        for cfg_dir in sorted(glob.glob(os.path.join(eval_dir, "*"))):
            if not os.path.isdir(cfg_dir):
                continue
            for run_dir in sorted(glob.glob(os.path.join(cfg_dir, "run-*"))):
                gp = os.path.join(run_dir, "grading.json")
                if not os.path.exists(gp):
                    print(f"warning: no grading.json in {run_dir}", file=sys.stderr)
                    continue
                g = json.load(open(gp))
                s = g.get("summary", {})
                passed, total = s.get("passed"), s.get("total")
                if passed is None or total is None:
                    exps = g.get("expectations", [])
                    passed, total = sum(1 for e in exps if e.get("passed")), len(exps)
                pr = s.get("pass_rate")
                if not isinstance(pr, (int, float)) or not total:
                    pr = passed / total if total else 0.0
                tp = os.path.join(run_dir, "timing.json")
                timing = json.load(open(tp)) if os.path.exists(tp) else {}
                runs.append({
                    "eval_id": eid, "eval_name": evals[eid], "configuration": os.path.basename(cfg_dir),
                    "run_number": int(run_dir.rsplit("-", 1)[1]),
                    "result": {
                        "pass_rate": round(float(pr), 4), "passed": passed, "failed": total - passed, "total": total,
                        "time_seconds": timing.get("total_duration_seconds", g.get("timing", {}).get("executor_duration_seconds", 0.0)),
                        "tokens": timing.get("total_tokens", 0), "cost_usd": timing.get("total_cost_usd"),
                        "tool_calls": timing.get("total_tool_calls", g.get("execution_metrics", {}).get("total_tool_calls", 0)),
                        "errors": timing.get("tool_errors", g.get("execution_metrics", {}).get("errors_encountered", 0)),
                        "status": timing.get("status", "ok"), "models": timing.get("models", []),
                    },
                    "expectations": g.get("expectations", []),
                    "notes": g.get("grader", {}).get("notes", []),
                })
    return runs, evals


def summarize(runs):
    configs = sorted({r["configuration"] for r in runs}, key=lambda c: (ORDER.index(c) if c in ORDER else 99, c))
    summary = {}
    for c in configs:
        rs = [r for r in runs if r["configuration"] == c]
        summary[c] = {
            "pass_rate": stats([r["result"]["pass_rate"] for r in rs]),
            "time_seconds": stats([r["result"]["time_seconds"] for r in rs]),
            "tokens": stats([r["result"]["tokens"] for r in rs]),
            "cost_usd": stats([r["result"]["cost_usd"] or 0 for r in rs]),
            "checks_passed": sum(r["result"]["passed"] for r in rs),
            "checks_total": sum(r["result"]["total"] for r in rs),
            "runs": len(rs),
        }
    if configs:
        base = summary[configs[0]]
        for c in configs[1:]:
            summary.setdefault("delta", {})[f"{configs[0]} - {c}"] = {
                "pass_rate": f"{base['pass_rate']['mean'] - summary[c]['pass_rate']['mean']:+.2f}",
                "time_seconds": f"{base['time_seconds']['mean'] - summary[c]['time_seconds']['mean']:+.1f}",
                "tokens": f"{base['tokens']['mean'] - summary[c]['tokens']['mean']:+.0f}",
            }
    return configs, summary


def fmt(st, scale=1.0, unit="", digits=0):
    if st["n"] == 0:
        return "—"
    mean = f"{st['mean'] * scale:.{digits}f}{unit}"
    return f"{mean} (n=1)" if st["n"] == 1 else f"{mean} ± {st['stddev'] * scale:.{digits}f}{unit}"


def markdown(bench, config_notes):
    md, cfgs, rs = bench["metadata"], bench["configurations"], bench["run_summary"]
    label = {c: c.replace("_", " ") for c in cfgs}
    lines = [f"# Skill benchmark: {md['skill_name']}", "",
             f"**Executor model**: {md['executor_model']}  ", f"**Grader**: {md['grader']}  ",
             f"**Date**: {md['timestamp']}  ",
             f"**Evals**: {', '.join(f'{k} {v}' for k, v in md['evals_run'].items())} — {md['runs_per_configuration']} run(s) per configuration  ", ""]
    if config_notes:
        lines += ["## Configurations", ""] + [f"- **{label.get(c, c)}** — {d}" for c, d in config_notes.items() if c in cfgs] + [""]
    lines += ["## Summary", "", "| Metric | " + " | ".join(label[c] for c in cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    lines.append("| Checks passed | " + " | ".join(f"**{rs[c]['checks_passed']}/{rs[c]['checks_total']}**" for c in cfgs) + " |")
    lines.append("| Pass rate | " + " | ".join(fmt(rs[c]["pass_rate"], 100, "%") for c in cfgs) + " |")
    lines.append("| Time per eval | " + " | ".join(fmt(rs[c]["time_seconds"], 1, " s") for c in cfgs) + " |")
    lines.append("| Output tokens per eval | " + " | ".join(fmt(rs[c]["tokens"]) for c in cfgs) + " |")
    lines.append("| Cost per eval (USD, list price) | " + " | ".join(fmt(rs[c]["cost_usd"], 1, "", 2) for c in cfgs) + " |")
    if "delta" in rs:
        lines += ["", "Deltas (first configuration minus the others): " + "; ".join(
            f"{k}: pass rate {v['pass_rate']}, time {v['time_seconds']} s, tokens {v['tokens']}" for k, v in rs["delta"].items())]
    lines += ["", "## Per eval", "", "| Eval | " + " | ".join(label[c] for c in cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for eid, name in md["evals_run"].items():
        row = []
        for c in cfgs:
            rr = [r for r in bench["runs"] if r["configuration"] == c and str(r["eval_id"]) == str(eid)]
            row.append(", ".join(f"{r['result']['passed']}/{r['result']['total']} ({r['result']['time_seconds']:.0f} s)" for r in rr) or "not run")
        lines.append(f"| {eid} {name} | " + " | ".join(row) + " |")
    fails = [(r, e) for r in bench["runs"] for e in r["expectations"] if not e.get("passed")]
    lines += ["", "## Failed checks", ""]
    lines += [f"- **eval {r['eval_id']} / {label.get(r['configuration'], r['configuration'])}** — {e['text']}  \n  {e.get('evidence', '')}" for r, e in fails] or ["None."]
    if bench.get("notes"):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in bench["notes"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace")
    ap.add_argument("--skill-name", default="eagle-pcb-studio")
    ap.add_argument("--grader", default="scripts (check_consistency.py, analyze_board.py, XML parsing) for mechanical checks; Claude grader model for the rest")
    ap.add_argument("--note", action="append", default=[], help="free-text note appended to the benchmark")
    ap.add_argument("--config-note", action="append", default=[], metavar="CONFIG=TEXT", help="describe a configuration")
    ap.add_argument("--output", "-o", help="benchmark.json path (default <workspace>/benchmark.json; .md written beside it)")
    a = ap.parse_args(argv)
    runs, evals = load_runs(a.workspace)
    if not runs:
        sys.exit(f"no grading.json found under {a.workspace}; refusing to write an empty benchmark")
    configs, summary = summarize(runs)
    models = sorted({m for r in runs for m in r["result"]["models"]})
    bench = {
        "metadata": {
            "skill_name": a.skill_name, "executor_model": ", ".join(models) or "<unknown>", "grader": a.grader,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evals_run": {str(k): v for k, v in sorted(evals.items()) if any(r["eval_id"] == k for r in runs)},
            "runs_per_configuration": max(s["runs"] for c, s in summary.items() if c != "delta") // max(1, len({r["eval_id"] for r in runs})),
            "tokens_definition": "output tokens of the executor run (input and cache tokens are in each run's timing.json)",
        },
        "configurations": configs,
        "runs": runs,
        "run_summary": summary,
        "notes": a.note,
    }
    out_json = a.output or os.path.join(a.workspace, "benchmark.json")
    json.dump(bench, open(out_json, "w"), indent=2)
    out_md = os.path.splitext(out_json)[0] + ".md"
    open(out_md, "w").write(markdown(bench, dict(s.split("=", 1) for s in a.config_note)))
    print(f"wrote {out_json}\nwrote {out_md}")
    for c in configs:
        s = summary[c]
        print(f"  {c:14s} {s['checks_passed']}/{s['checks_total']} checks, pass rate {s['pass_rate']['mean'] * 100:.0f}%, {s['time_seconds']['mean']:.0f} s mean")


if __name__ == "__main__":
    main()
