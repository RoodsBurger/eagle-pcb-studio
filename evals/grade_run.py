#!/usr/bin/env python3
"""Grade one eval run: tool-verified checks first, then a Claude grader for the remaining assertions.

Writes <run-dir>/grading.json in the skill-creator schema (expectations[], summary, execution_metrics, timing).
Tool-verified checks use the repository's own analyzers as oracles (check_consistency.py, analyze_board.py) and
plain XML parsing; the grader model only judges what cannot be checked mechanically, and it sees the
tool-verified facts.

Usage:  python3 evals/grade_run.py <run-dir> [--model claude-fable-5-1] [--config-dir DIR]
        python3 evals/grade_run.py --all <workspace>          # every run-* without grading.json
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS = os.path.join(REPO, "scripts")

GRADER_SCHEMA = {
    "type": "object",
    "properties": {
        "expectations": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "passed": {"type": "boolean"}, "evidence": {"type": "string"}},
            "required": ["text", "passed", "evidence"]}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["expectations", "notes"],
}


def sh(args, cwd=None, timeout=300):
    p = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)


def rel(path, run_dir):
    return os.path.relpath(path, run_dir)


def candidates(run_dir, ext):
    """Output files with the extension, newest first; an input edited in place counts too."""
    files = glob.glob(os.path.join(run_dir, "outputs", "**", f"*{ext}"), recursive=True)
    for p in glob.glob(os.path.join(run_dir, "inputs", f"*{ext}")):
        if os.path.getmtime(p) > os.path.getmtime(os.path.join(run_dir, "prompt.txt")):
            files.append(p)
    return sorted(set(files), key=os.path.getmtime, reverse=True)


def eagle_ok(path):
    raw = open(path, errors="replace").read(400)
    try:
        ET.parse(path)
    except ET.ParseError as e:
        return False, f"does not parse: {e}"
    if "<!DOCTYPE eagle" not in raw:
        return False, "parses but the <!DOCTYPE eagle> declaration is missing"
    return True, "parses and keeps <!DOCTYPE eagle SYSTEM \"eagle.dtd\">"


def consistency(sch, brd):
    code, out = sh([sys.executable, os.path.join(SCRIPTS, "check_consistency.py"), sch, brd])
    m = re.search(r"PARTS\s+sch=(\d+)\s+brd=(\d+)", out)
    n = re.search(r"NETS\s+sch=(\d+)\s+brd=(\d+)", out)
    bad = re.search(r"inconsistent \(ERC errors\): (.*)", out)
    return {"pass": "VERDICT: PASS" in out, "parts": m.groups() if m else None, "nets": n.groups() if n else None,
            "inconsistent": bad.group(1).strip() if bad else "", "text": out}


def sch_parts(sch):
    root = ET.parse(sch).getroot()
    return {p.get("name") for p in root.iter("part")}


def brd_elements(brd):
    root = ET.parse(brd).getroot()
    return {e.get("name") for e in root.iter("element")}


def brd_signals(brd):
    root = ET.parse(brd).getroot()
    return {s.get("name"): len(list(s.iter("contactref"))) for s in root.iter("signal")}


def sch_nets(sch):
    root = ET.parse(sch).getroot()
    return {n.get("name") for n in root.iter("net")}


def board_outline_and_elements(brd):
    """Board outline bbox (layer 20 wires) and element positions, in mm."""
    root = ET.parse(brd).getroot()
    xs, ys = [], []
    for w in root.iter("wire"):
        if w.get("layer") == "20":
            xs += [float(w.get("x1")), float(w.get("x2"))]
            ys += [float(w.get("y1")), float(w.get("y2"))]
    outline = (min(xs), min(ys), max(xs), max(ys)) if xs else None
    els = {e.get("name"): (float(e.get("x")), float(e.get("y"))) for e in root.iter("element")}
    return outline, els


# ---------------------------------------------------------------- tool-verified checks per eval

def check_eval1(run_dir, meta):
    """ERC inconsistent footprints: a corrected .sch consistent with the board, valid XML, netlist unchanged."""
    brd = os.path.join(run_dir, "inputs", "design.brd")
    res = {}
    schs = candidates(run_dir, ".sch")
    best = None
    for s in schs:
        ok, why = eagle_ok(s)
        c = consistency(s, brd) if ok else None
        if c and c["pass"]:
            best = (s, ok, why, c)
            break
        best = best or (s, ok, why, c)
    A = meta["assertions"]
    if not best:
        res[A[2]] = (False, "no .sch was written to outputs/ (and inputs/design.sch is unchanged)")
        res[A[3]] = (False, "no corrected .sch produced")
        res[A[4]] = (False, "no corrected .sch produced")
        return res
    s, ok, why, c = best
    name = rel(s, run_dir)
    res[A[3]] = (ok, f"{name}: {why}")
    if c:
        res[A[2]] = (c["pass"], f"check_consistency.py {name} inputs/design.brd -> {'PASS' if c['pass'] else 'FAIL'}"
                     + (f"; still inconsistent: {c['inconsistent']}" if c["inconsistent"] and c["inconsistent"] != "NONE" else ""))
        net_ok = c["parts"] == ("71", "71") and c["nets"] == ("57", "57")
        res[A[4]] = (net_ok, f"check_consistency.py: PARTS sch={c['parts'][0]} brd={c['parts'][1]}, NETS sch={c['nets'][0]} brd={c['nets'][1]}" if c["parts"] and c["nets"] else "part/net counts not reported")
    else:
        res[A[2]] = (False, f"{name} does not parse, consistency not checkable")
        res[A[4]] = (False, f"{name} does not parse")
    return res


def check_eval2(run_dir, meta):
    """Placement task: a valid SVG preview; geometry from a placements JSON when one exists."""
    A = meta["assertions"]
    res = {}
    svgs = candidates(run_dir, ".svg")
    if not svgs:
        res[A[4]] = (False, "no .svg in outputs/")
    else:
        svg = svgs[0]
        try:
            root = ET.parse(svg).getroot()
            shapes = [e for e in root.iter() if e.tag.split('}')[-1] in ("rect", "polygon", "path", "circle", "polyline", "line")]
            texts = [(e.text or "").strip() for e in root.iter() if e.tag.split('}')[-1] == "text"]
            labels = sorted({t for t in texts if t in {"U1", "U2", "C1", "C2", "C3", "C4", "L1", "J1", "J2"}})
            ok = len(shapes) >= 9
            res[A[4]] = (ok, f"{rel(svg, run_dir)} parses: {len(shapes)} shapes, part labels found: {labels or 'none'}")
        except ET.ParseError as e:
            res[A[4]] = (False, f"{rel(svg, run_dir)} does not parse: {e}")
    # geometry from a placements JSON (place_components.py output or any [{name,x,y,w,h}] list)
    for p in candidates(run_dir, ".json"):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        pl = d.get("placements") if isinstance(d, dict) else d
        if not isinstance(pl, list) or not pl or not all(isinstance(x, dict) and "name" in x for x in pl):
            continue
        spec = None
        for sp in candidates(run_dir, ".json"):
            try:
                sd = json.load(open(sp))
                if isinstance(sd, dict) and "parts" in sd and "board" in sd:
                    spec = sd
                    break
            except Exception:
                pass
        if spec is None:
            continue
        sizes = {x["name"]: (x["w"], x["h"]) for x in spec["parts"]}
        W, H = spec["board"]["w"], spec["board"]["h"]
        rects = {}
        for x in pl:
            w, h = sizes.get(x["name"], (None, None))
            if w is None:
                continue
            if x.get("rot", 0) in (90, 270):
                w, h = h, w
            rects[x["name"]] = (x["x"] - w / 2, x["y"] - h / 2, x["x"] + w / 2, x["y"] + h / 2)
        names = set(rects)
        want = {"U1", "U2", "C1", "C2", "C3", "C4", "L1", "J1", "J2"}
        res[A[0]] = (want <= names, f"{rel(p, run_dir)} places {sorted(names)}")
        ov = [(a, b) for a in rects for b in rects if a < b and not (
            rects[a][2] <= rects[b][0] + 1e-6 or rects[b][2] <= rects[a][0] + 1e-6 or
            rects[a][3] <= rects[b][1] + 1e-6 or rects[b][3] <= rects[a][1] + 1e-6)]
        res[A[1]] = (not ov, f"rectangle overlap test on {len(rects)} parts: {ov or 'none'}")
        inside = all(r[0] >= -1e-6 and r[1] >= -1e-6 and r[2] <= W + 1e-6 and r[3] <= H + 1e-6 for r in rects.values())
        res[A[2]] = (inside, f"all centers+sizes within {W} x {H} mm: {inside}")
        def on_edge(r):
            return abs(r[0]) < 0.5 or abs(r[1]) < 0.5 or abs(W - r[2]) < 0.5 or abs(H - r[3]) < 0.5
        je = {n: on_edge(rects[n]) for n in ("J1", "J2") if n in rects}
        res[A[3]] = (bool(je) and all(je.values()), f"edge test (within 0.5 mm of an outline edge): {je}")
        break
    return res


def check_eval3(run_dir, meta):
    """Schematic -> placed board: valid .brd, every part placed, nets as signals, consistent libraries, SVG."""
    A = meta["assertions"]
    res = {}
    sch = os.path.join(run_dir, "inputs", "design.sch")
    brds = candidates(run_dir, ".brd")
    if not brds:
        for i in range(4):
            res[A[i]] = (False, "no .brd in outputs/")
    else:
        brd = brds[0]
        name = rel(brd, run_dir)
        ok, why = eagle_ok(brd)
        res[A[0]] = (ok, f"{name}: {why}")
        if ok:
            parts, els = sch_parts(sch), brd_elements(brd)
            missing = sorted(parts - els)
            code, out = sh([sys.executable, os.path.join(SCRIPTS, "analyze_board.py"), brd, "-o", os.path.join(run_dir, "grader_board.json"), "--text"])
            m = re.search(r"overlaps:\s*(\d+)", out)
            overlaps = int(m.group(1)) if m else None
            outline, pos = board_outline_and_elements(brd)
            outside = [n for n, (x, y) in pos.items() if outline and not (outline[0] - 0.01 <= x <= outline[2] + 0.01 and outline[1] - 0.01 <= y <= outline[3] + 0.01)]
            placed_ok = not missing and overlaps == 0 and not outside
            res[A[1]] = (placed_ok, f"{len(els)} elements for {len(parts)} schematic parts (missing: {missing or 'none'}); "
                                    f"analyze_board.py overlaps: {overlaps}; element origins outside outline {outline}: {outside or 'none'}")
            nets, sigs = sch_nets(sch), brd_signals(brd)
            missing_n = sorted(nets - set(sigs))
            empty = sorted(n for n, k in sigs.items() if k == 0 and n in nets)
            res[A[2]] = (not missing_n and not empty, f"{len(sigs)} signals for {len(nets)} schematic nets (missing: {missing_n or 'none'}; "
                                                      f"signals without contactrefs: {empty or 'none'}); contactrefs total {sum(sigs.values())}")
            c = consistency(sch, brd)
            res[A[3]] = (c["pass"], f"check_consistency.py inputs/design.sch {name} -> {'PASS' if c['pass'] else 'FAIL'}"
                         + (f"; inconsistent: {c['inconsistent']}" if c["inconsistent"] and c["inconsistent"] != "NONE" else ""))
        else:
            for i in (1, 2, 3):
                res[A[i]] = (False, f"{name} is not valid EAGLE XML")
    svgs = candidates(run_dir, ".svg")
    if not svgs:
        res[A[4]] = (False, "no .svg in outputs/")
    else:
        try:
            root = ET.parse(svgs[0]).getroot()
            shapes = sum(1 for e in root.iter() if e.tag.split('}')[-1] in ("rect", "polygon", "path", "circle", "polyline", "line"))
            res[A[4]] = (shapes > 10, f"{rel(svgs[0], run_dir)} parses with {shapes} shapes")
        except ET.ParseError as e:
            res[A[4]] = (False, f"{rel(svgs[0], run_dir)} does not parse: {e}")
    return res


def check_eval0(run_dir, meta):
    """Gerber review: facts the transcript must contain; the verdict and the dam call go to the grader."""
    A = meta["assertions"]
    text = open(os.path.join(run_dir, "transcript.md"), errors="replace").read()
    res = {}
    size = bool(re.search(r"54(\.\d+)?\s*(mm)?\s*[x×by]+\s*51(\.\d+)?", text)) or bool(re.search(r"51(\.\d+)?\s*(mm)?\s*[x×by]+\s*54(\.\d+)?", text))
    res[A[1]] = (size, "board size 54 x 51 mm stated in the transcript" if size else "no 54 x 51 mm board size in the transcript")
    drill = bool(re.search(r"0\.2\s*mm", text))
    res[A[3]] = (drill, "0.2 mm drill mentioned (grader confirms no false drill error)" if drill else "smallest drill 0.2 mm never mentioned") if drill else (False, "smallest drill 0.2 mm never mentioned")
    return res


CHECKS = {0: check_eval0, 1: check_eval1, 2: check_eval2, 3: check_eval3}


# ---------------------------------------------------------------- grader model

def list_outputs(run_dir, limit=12000):
    lines = []
    for p in sorted(glob.glob(os.path.join(run_dir, "outputs", "**", "*"), recursive=True)):
        if os.path.isdir(p):
            continue
        size = os.path.getsize(p)
        head = ""
        if p.endswith((".md", ".txt", ".json", ".svg", ".csv")) and size < 400000:
            head = open(p, errors="replace").read(2500)
        lines.append(f"- {rel(p, run_dir)} ({size} bytes)" + (f"\n```\n{head}\n```" if head else ""))
    return "\n".join(lines)[:limit] or "(no files in outputs/)"


def grade_with_model(run_dir, meta, config, verified, model, config_dir, timeout=900):
    transcript = open(os.path.join(run_dir, "transcript.md"), errors="replace").read()
    if len(transcript) > 220000:
        transcript = transcript[:110000] + "\n\n[... middle of transcript omitted ...]\n\n" + transcript[-110000:]
    facts = "\n".join(f"- [{'PASS' if ok else 'FAIL'}] {t}\n  evidence: {ev}" for t, (ok, ev) in verified.items()) or "(none)"
    pending = [a for a in meta["assertions"] if a not in verified]
    prompt = f"""You are grading one run of a PCB-design task performed by an AI agent. Judge ONLY the assertions listed under
"Assertions to grade" from the transcript and output listing below. Be strict: PASS needs explicit evidence of genuine
completion in the transcript or outputs, not a claim. Quote the evidence briefly. Do not use tools; everything you need is here.

Task prompt given to the agent (configuration: {config}):
<<<
{meta['plain_prompt'] if config == 'no_skill' else meta['prompt']}
>>>

Tool-verified facts already established by scripts (do not re-grade these, but use them as context):
{facts}

Assertions to grade (return exactly these texts, in this order):
{json.dumps(pending, indent=2)}

Output files written by the agent:
{list_outputs(run_dir)}

Transcript:
<<<
{transcript}
>>>

Also return 1-3 short notes on anything a reviewer should know (e.g. a correct-looking claim the outputs contradict,
or an assertion that is trivially satisfied)."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(GRADER_SCHEMA), "--max-turns", "1",
           "--no-session-persistence", "--tools", ""]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=run_dir)
    dur = time.time() - t0
    line = next((l for l in p.stdout.splitlines() if l.startswith("{")), "{}")
    res = json.loads(line)
    out = res.get("structured_output")
    if not out:
        try:
            out = json.loads(res.get("result", "{}"))
        except Exception:
            raise RuntimeError(f"grader returned no structured output: {res.get('result', p.stderr)[:500]}")
    models = list(res.get("modelUsage", {}).keys())
    return out, dur, models, res.get("total_cost_usd")


def grade_run(run_dir, model=None, config_dir=None):
    eval_dir = os.path.dirname(os.path.dirname(run_dir))
    config = os.path.basename(os.path.dirname(run_dir))
    meta = json.load(open(os.path.join(eval_dir, "eval_metadata.json")))
    timing = json.load(open(os.path.join(run_dir, "timing.json")))
    verified = CHECKS.get(meta["eval_id"], lambda r, m: {})(run_dir, meta)
    model_out, gdur, gmodels, gcost = {"expectations": [], "notes": []}, 0.0, [], None
    if any(a not in verified for a in meta["assertions"]):
        model_out, gdur, gmodels, gcost = grade_with_model(run_dir, meta, config, verified, model, config_dir)
    by_text = {e["text"]: e for e in model_out.get("expectations", [])}
    expectations = []
    for a in meta["assertions"]:
        if a in verified:
            ok, ev = verified[a]
            expectations.append({"text": a, "passed": bool(ok), "evidence": ev, "verified_by": "script"})
        elif a in by_text:
            e = by_text[a]
            expectations.append({"text": a, "passed": bool(e["passed"]), "evidence": e["evidence"], "verified_by": "grader-model"})
        else:
            expectations.append({"text": a, "passed": False, "evidence": "grader returned no verdict for this assertion", "verified_by": "none"})
    passed = sum(1 for e in expectations if e["passed"])
    total = len(expectations)
    grading = {
        "eval_id": meta["eval_id"], "eval_name": meta["eval_name"], "configuration": config,
        "expectations": expectations,
        "summary": {"passed": passed, "failed": total - passed, "total": total, "pass_rate": round(passed / total, 4) if total else 0.0},
        "execution_metrics": {"total_tool_calls": timing.get("total_tool_calls", 0), "errors_encountered": timing.get("tool_errors", 0),
                              "output_chars": os.path.getsize(os.path.join(run_dir, "transcript.md")), "status": timing.get("status")},
        "timing": {"executor_duration_seconds": timing.get("total_duration_seconds", 0.0), "grader_duration_seconds": round(gdur, 1),
                   "total_duration_seconds": timing.get("total_duration_seconds", 0.0)},
        "grader": {"models": gmodels, "cost_usd": gcost, "notes": model_out.get("notes", [])},
    }
    json.dump(grading, open(os.path.join(run_dir, "grading.json"), "w"), indent=2)
    return grading


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a run directory, or a workspace with --all")
    ap.add_argument("--all", action="store_true", help="grade every run-* under the workspace that lacks grading.json")
    ap.add_argument("--force", action="store_true", help="re-grade even when grading.json exists")
    ap.add_argument("--model", default=None)
    ap.add_argument("--config-dir", default=None, help="isolated CLAUDE_CONFIG_DIR for the grader (default <workspace>/.claude-config if present)")
    a = ap.parse_args(argv)
    # The grader subprocess runs with cwd=run_dir, so every path it receives must be absolute.
    a.target = os.path.abspath(a.target)
    if a.config_dir:
        a.config_dir = os.path.abspath(a.config_dir)
    runs = sorted(glob.glob(os.path.join(a.target, "eval-*", "*", "run-*"))) if a.all else [a.target]
    for r in runs:
        if not os.path.exists(os.path.join(r, "timing.json")):
            print(f"skip {r}: not finished")
            continue
        if os.path.exists(os.path.join(r, "grading.json")) and not a.force:
            print(f"skip {r}: graded")
            continue
        cfg = a.config_dir
        if cfg is None:
            ws = a.target if a.all else os.path.dirname(os.path.dirname(os.path.dirname(r)))
            cand = os.path.join(ws, ".claude-config")
            cfg = cand if os.path.isdir(cand) else None
        g = grade_run(r, a.model, cfg)
        print(f"{r}: {g['summary']['passed']}/{g['summary']['total']}")
        for e in g["expectations"]:
            print(f"   [{'PASS' if e['passed'] else 'FAIL'}|{e['verified_by']}] {e['text'][:90]}")


if __name__ == "__main__":
    main()
