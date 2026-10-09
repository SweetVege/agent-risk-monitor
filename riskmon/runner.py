"""Run one task unattended: start the agent with `claude -p`, with nobody to answer prompts and the monitor as the only gate.

    riskmon run --cwd ~/code/my-project "make the tests pass"

The target project needs the hooks installed already (riskmon init).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

# let the agent act freely; otherwise Claude Code's own permission system stops actions first and the monitor sees nothing
TOOLS = "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch"
TIMEOUT_SECONDS = 20 * 60


def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.load(resp)


def find_cli() -> str | None:
    return shutil.which("claude") or next(
        (str(p) for p in [Path.home() / ".local" / "bin" / "claude"] if p.exists()), None
    )


def run(task: str, cwd: str, port: int, max_turns: int | None = None, hosts: list | None = None, dirs: list | None = None,
        observe: bool = False) -> int:
    cli = find_cli()
    if cli is None:
        print("The claude CLI was not found. Install it first: curl -fsSL https://claude.ai/install.sh | bash")
        return 2
    try:
        _get(port, "/healthz")
    except OSError:
        print("The daemon is not running. In an unattended run it is the only gate; start it first: riskmon serve")
        return 2
    cwd = str(Path(cwd).expanduser().resolve())
    if not (Path(cwd) / ".claude" / "settings.json").exists():
        print(f"{cwd} has no .claude/settings.json, so the hooks would not fire. Run: riskmon init --project {cwd}")
        return 2

    cmd = [cli, "-p", task, "--output-format", "json", "--permission-prompts", "none", "--allowedTools", TOOLS]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    # Extra hosts and directories for this run. An agent cannot change its parent's environment, so it cannot widen its own scope
    declared = {"hosts": hosts or [], "dirs": [str(Path(d).expanduser().resolve()) for d in dirs or []]}
    env = {**os.environ, "RISKMON_MODE": "unattended", "RISKMON_SCOPE": json.dumps(declared)}
    if observe:
        env["RISKMON_OBSERVE"] = "1"
        print("Observe mode: actions that would be held are let through and recorded. Denials and suspensions still apply.")
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"Stopped after {TIMEOUT_SECONDS // 60} minutes.")
        return 1

    try:
        out = json.loads(proc.stdout)
    except ValueError:
        print(f"claude exited with {proc.returncode} and its output was not JSON:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        return 1

    sid = out.get("session_id", "")
    print(f"Session {sid}  turns {out.get('num_turns')}  cost ${out.get('total_cost_usd', 0):.3f}")
    events = list(reversed(_get(port, f"/api/events?session_id={sid}"))) if sid else []
    for e in events:
        d = e["detail"]
        status = (d.get("outcome") or {}).get("status", "-")
        print(f"  #{e['id']} {e['verdict']:7} {e['score']:3} {e['tier']:5} {status:6} {e['tool']}: {e['target'][:140]!r}")
        why = "; ".join(d["combos"] + d["findings"] + ([f"judge: {d['rationale']}"] if d["rationale"] else []))
        if e["verdict"] != "allow" or e["score"] >= 10:
            print(f"      {why}")
    # permission_denials includes calls the monitor stopped; subtracting those leaves what Claude Code itself refused
    denials = len(out.get("permission_denials") or [])
    ours = sum(e["verdict"] != "allow" for e in events)
    if denials > ours:
        print(f"Apart from the monitor, Claude Code's own permission system refused {denials - ours} calls.")
    if sid:
        from . import report

        print("\n== Activity report ==\n" + report.render(_get(port, f"/api/report?session_id={sid}")))
    print("\nThe agent's final answer:\n" + str(out.get("result", "")).strip())
    return 0
