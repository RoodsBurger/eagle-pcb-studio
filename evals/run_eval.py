#!/usr/bin/env python3
"""Headless eval runner: executes the evals in evals.json with the Claude Code CLI under three configurations.

Configurations
  with_skill     the eval prompt; the skill is installed in the run's .claude/skills/ and named in the prompt
  strong_prompt  the same detailed eval prompt, no skill anywhere
  no_skill       the eval's plain_prompt (a short request without the coaching the eval prompt carries), no skill

Each run gets its own directory, an isolated CLAUDE_CONFIG_DIR (credentials only: no user skills, plugins,
hooks, memory or CLAUDE.md), the fixtures copied into inputs/, and an empty outputs/. Skills and MCP servers are
disabled for every configuration (account-level plugins would otherwise load), so the with_skill run reads the
installed SKILL.md by path and follows it — the same method skill-creator's with-skill subagents use.

Layout (skill-creator compatible):  <workspace>/eval-<id>/<config>/run-<n>/{transcript.jsonl, transcript.md,
result.json, timing.json, inputs/, outputs/}  plus <workspace>/eval-<id>/eval_metadata.json

Usage
  python3 evals/run_eval.py --config no_skill --eval 1 2 3 --fixtures evals/fixtures --workspace evals/workspace/<date>
  python3 evals/run_eval.py --config with_skill strong_prompt no_skill --eval 1 2 3 --parallel 3
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CONFIGS = ("with_skill", "strong_prompt", "no_skill")
SKILL_FILES = ("SKILL.md", "LICENSE", "scripts", "references")
OUTPUT_RULE = ("\n\nWrite every file you create or modify to ./outputs/ (keep ./inputs/ unchanged) and finish with a "
               "summary of what you found and did.")
WITH_SKILL_PREFIX = ("A Claude skill is installed at .claude/skills/eagle-pcb-studio/ — read its SKILL.md first and follow "
                     "it for this task.\n\n")


def load_evals():
    with open(os.path.join(HERE, "evals.json")) as f:
        return json.load(f)


def ensure_config_dir(path):
    """Build an isolated Claude config dir holding only the login credential and onboarding state."""
    os.makedirs(path, exist_ok=True)
    cred = os.path.join(path, ".credentials.json")
    if not os.path.exists(cred):
        src = None
        if sys.platform == "darwin":
            try:
                out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                     capture_output=True, text=True, timeout=20).stdout.strip()
                json.loads(out)
                src = out
            except Exception:
                src = None
        if src is None:
            home_cred = os.path.expanduser("~/.claude/.credentials.json")
            if os.path.exists(home_cred):
                src = open(home_cred).read()
        if src is None:
            sys.exit("no Claude Code credential found: run `claude` once and log in, or set ANTHROPIC_API_KEY")
        with open(cred, "w") as f:
            f.write(src)
        os.chmod(cred, 0o600)
    cfg = os.path.join(path, ".claude.json")
    if not os.path.exists(cfg):
        state = {"hasCompletedOnboarding": True}
        try:
            home = json.load(open(os.path.expanduser("~/.claude.json")))
            for k in ("oauthAccount", "installMethod"):
                if k in home:
                    state[k] = home[k]
        except Exception:
            pass
        json.dump(state, open(cfg, "w"))
    settings = os.path.join(path, "settings.json")
    if not os.path.exists(settings):
        json.dump({"permissions": {"defaultMode": "bypassPermissions"}}, open(settings, "w"))
    warmup(path)
    return path


def warmup(config_dir):
    """One sequential call before any parallel run: concurrent first launches of a fresh config dir race on the
    credential migration and report "Not logged in"."""
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    env.pop("CLAUDECODE", None)
    p = subprocess.run(["claude", "-p", "Reply with exactly OK.", "--max-turns", "1", "--output-format", "json",
                        "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"],
                       capture_output=True, text=True, env=env, timeout=180)
    line = next((l for l in p.stdout.splitlines() if l.startswith("{")), "{}")
    res = json.loads(line) if line else {}
    if res.get("is_error") or "OK" not in str(res.get("result", "")):
        sys.exit(f"warm-up call failed in {config_dir}: {res.get('result', p.stderr[:300])}")


def install_skill(run_dir):
    dst = os.path.join(run_dir, ".claude", "skills", "eagle-pcb-studio")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    for name in SKILL_FILES:
        src = os.path.join(REPO, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dst, name), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif os.path.exists(src):
            shutil.copy2(src, dst)
    return dst


def stage_inputs(ev, fixtures, run_dir):
    """Copy the eval's fixture files into inputs/ and return placeholder -> absolute path."""
    inputs = os.path.join(run_dir, "inputs")
    os.makedirs(inputs, exist_ok=True)
    subst = {}
    for rel in ev.get("files", []):
        src = os.path.join(fixtures, rel.replace("fixtures/", "", 1))
        if not os.path.exists(src):
            raise FileNotFoundError(f"fixture missing: {src} (see evals/fixtures/README.md)")
        dst = os.path.join(inputs, os.path.basename(src))
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            subst["<GERBER_DIR>"] = dst
        else:
            shutil.copy2(src, dst)
            subst["<SCH>" if dst.endswith(".sch") else "<BRD>" if dst.endswith(".brd") else "<FILE>"] = dst
    return subst


def build_prompt(ev, config, subst):
    text = ev["plain_prompt"] if config == "no_skill" else ev["prompt"]
    for k, v in subst.items():
        text = text.replace(k, v)
    if config == "with_skill":
        text = WITH_SKILL_PREFIX + text
    return text + OUTPUT_RULE


def transcript_markdown(events, max_chars=1500):
    """Readable transcript: assistant text, tool calls with truncated inputs, truncated tool results."""
    lines = []
    for ev in events:
        t = ev.get("type")
        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block["text"].strip():
                    lines.append(f"\n**Assistant:** {block['text'].strip()}\n")
                elif block.get("type") == "tool_use":
                    inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                    lines.append(f"\n`tool_use {block.get('name')}` {inp[:max_chars]}{' …' if len(inp) > max_chars else ''}\n")
        elif t == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    c = str(c or "")
                    lines.append(f"`tool_result{' (error)' if block.get('is_error') else ''}` {c[:max_chars]}{' …' if len(c) > max_chars else ''}\n")
        elif t == "result":
            lines.append(f"\n---\n**Result:** {str(ev.get('result', ''))[:6000]}\n")
    return "\n".join(lines)


def run_one(ev, config, run_dir, fixtures, config_dir, model, max_turns, budget, timeout_s):
    os.makedirs(os.path.join(run_dir, "outputs"), exist_ok=True)
    subst = stage_inputs(ev, fixtures, run_dir)
    if config == "with_skill":
        install_skill(run_dir)
    prompt = build_prompt(ev, config, subst)
    with open(os.path.join(run_dir, "prompt.txt"), "w") as f:
        f.write(prompt)
    # --disable-slash-commands: no Skill tool in any configuration, so account-level plugin skills cannot
    # interfere; the with_skill run reads SKILL.md by path (the skill-creator method). --strict-mcp-config: no MCP servers.
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
           "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
           "--max-turns", str(max_turns), "--max-budget-usd", str(budget)]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    env.pop("CLAUDECODE", None)
    t0 = time.time()
    events, status = [], "ok"
    with open(os.path.join(run_dir, "transcript.jsonl"), "w") as out, open(os.path.join(run_dir, "stderr.log"), "w") as err:
        proc = subprocess.Popen(cmd, cwd=run_dir, env=env, stdout=subprocess.PIPE, stderr=err, text=True, start_new_session=True)
        try:
            for line in proc.stdout:
                out.write(line)
                out.flush()
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
                if time.time() - t0 > timeout_s:
                    raise TimeoutError
            proc.wait(timeout=60)
        except (TimeoutError, subprocess.TimeoutExpired):
            status = "timeout"
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(3)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
    dur = time.time() - t0
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    usage = result.get("usage", {})
    tool_calls = sum(1 for e in events if e.get("type") == "assistant"
                     for b in e.get("message", {}).get("content", []) if b.get("type") == "tool_use")
    tool_errors = sum(1 for e in events if e.get("type") == "user"
                      for b in e.get("message", {}).get("content", []) if isinstance(b, dict) and b.get("is_error"))
    timing = {
        "status": status if not result.get("is_error") else f"error: {str(result.get('result'))[:200]}",
        "total_duration_seconds": round(dur, 1),
        "api_duration_seconds": round(result.get("duration_api_ms", 0) / 1000, 1),
        "num_turns": result.get("num_turns"),
        "total_tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "total_tokens": usage.get("output_tokens", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "total_cost_usd": result.get("total_cost_usd"),
        "models": list(result.get("modelUsage", {}).keys()),
        "command": cmd[:1] + ["-p", "<prompt>"] + cmd[3:],
    }
    json.dump(result, open(os.path.join(run_dir, "result.json"), "w"), indent=1)
    json.dump(timing, open(os.path.join(run_dir, "timing.json"), "w"), indent=1)
    with open(os.path.join(run_dir, "transcript.md"), "w") as f:
        f.write(f"# {ev['name']} — {config}\n\n**Prompt:**\n\n{prompt}\n\n---\n" + transcript_markdown(events))
    return timing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", choices=CONFIGS, required=True)
    ap.add_argument("--eval", nargs="+", type=int, required=True, help="eval ids from evals.json")
    ap.add_argument("--runs", type=int, default=1, help="runs per eval and configuration")
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--workspace", default=os.path.join(HERE, "workspace", datetime.now().strftime("%Y-%m-%d")))
    ap.add_argument("--config-dir", help="isolated CLAUDE_CONFIG_DIR (default <workspace>/.claude-config)")
    ap.add_argument("--model", default=None, help="model id passed to claude (default: the CLI default)")
    ap.add_argument("--max-turns", type=int, default=200)
    ap.add_argument("--budget", type=float, default=40.0, help="--max-budget-usd per run")
    ap.add_argument("--timeout", type=float, default=2400, help="seconds per run before the process tree is killed")
    ap.add_argument("--parallel", type=int, default=1)
    a = ap.parse_args(argv)

    evals = {e["id"]: e for e in load_evals()["evals"]}
    # Every run uses its own cwd, so all paths handed to subprocesses must be absolute.
    a.workspace, a.fixtures = os.path.abspath(a.workspace), os.path.abspath(a.fixtures)
    os.makedirs(a.workspace, exist_ok=True)
    config_dir = ensure_config_dir(os.path.abspath(a.config_dir or os.path.join(a.workspace, ".claude-config")))
    jobs = []
    for eid in a.eval:
        ev = evals[eid]
        if "plain_prompt" not in ev and "no_skill" in a.config:
            sys.exit(f"eval {eid} has no plain_prompt (needed for no_skill)")
        eval_dir = os.path.join(a.workspace, f"eval-{eid}")
        os.makedirs(eval_dir, exist_ok=True)
        json.dump({"eval_id": eid, "eval_name": ev["name"], "prompt": ev["prompt"], "plain_prompt": ev.get("plain_prompt"),
                   "assertions": ev["assertions"], "files": ev.get("files", [])},
                  open(os.path.join(eval_dir, "eval_metadata.json"), "w"), indent=2)
        for config in a.config:
            for n in range(1, a.runs + 1):
                run_dir = os.path.join(eval_dir, config, f"run-{n}")
                tp = os.path.join(run_dir, "timing.json")
                if os.path.exists(tp) and json.load(open(tp)).get("status") == "ok":
                    print(f"skip {run_dir} (done)")
                    continue
                if os.path.exists(run_dir):
                    shutil.rmtree(run_dir)
                os.makedirs(run_dir)
                jobs.append((ev, config, run_dir))

    def work(job):
        ev, config, run_dir = job
        time.sleep(5 * (jobs.index(job) % max(1, a.parallel)))
        print(f"start eval-{ev['id']} {config} -> {run_dir}", flush=True)
        t = run_one(ev, config, run_dir, a.fixtures, config_dir, a.model, a.max_turns, a.budget, a.timeout)
        print(f"done  eval-{ev['id']} {config}: {t['status']} {t['total_duration_seconds']}s "
              f"{t['total_tool_calls']} tool calls, {t['total_tokens']} output tokens, ${t['total_cost_usd']}", flush=True)
        return t

    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        list(pool.map(work, jobs))
    print("all runs finished:", a.workspace)


if __name__ == "__main__":
    main()
