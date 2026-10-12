"""Claude Code hook: hand each action to the local riskmon daemon for scoring and turn its verdict into a Claude Code decision.

Claude Code lets a tool call through when a hook times out or crashes, so this keeps its own
timeout and its own fallback: with the daemon unavailable, out-of-bounds actions are denied
and the rest go through.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from . import config, rules
from . import repo
from .events import from_claude_code

HTTP_TIMEOUT = 12  # must stay below the hook timeout configured in settings.json


def client() -> dict:
    """Where this machine's hooks send actions: the team's daemon if it has joined one, otherwise the one on this machine.

    The environment wins over the file, for runners that are configured that way and keep nothing on disk.
    """
    server, token = os.environ.get("RISKMON_SERVER", ""), os.environ.get("RISKMON_TOKEN", "")
    if not server:
        try:
            saved = json.loads((config.home() / "client.json").read_text(encoding="utf-8"))
            server, token = str(saved.get("server", "")), str(saved.get("token", ""))
        except (OSError, ValueError):
            pass
    return {"server": server.rstrip("/"), "token": token}


def ask_daemon(payload: dict, port: int) -> dict:
    where = client()
    headers = {"Content-Type": "application/json"}
    if where["server"]:
        headers["Authorization"] = "Bearer " + where["token"]
        payload = {**payload, "riskmon_user": os.environ.get("USER", "")}
    req = urllib.request.Request(
        (where["server"] or f"http://127.0.0.1:{port}") + "/v1/hook",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def refusal(verdict: str, event_id, note: str = "") -> str:
    """What the agent is told when an action does not run. `note` is a scope limit the operator declared, if that is what stopped it."""
    if verdict == "suspend":
        return "This session is suspended by the risk monitor. Stop and tell the user to review it."
    if verdict == "reviewed":
        return (
            f"A reviewer looked at this action and kept it refused (event #{event_id}). " + (note + " " if note else "")
            + "Do not retry it or look for another way to do the same thing. If the task can be done without it, do that; otherwise stop and report."
        )
    if verdict == "hold":
        return (
            f"Held for human review by the risk monitor (event #{event_id}). " + (note + " " if note else "")
            + "Nobody is available to approve it right now. Do not retry it or look for another way to do the same thing. "
            "Carry on with the parts of the task that do not need it; if nothing is left, stop and report exactly what is waiting on review."
        )
    if note:
        # a declared limit, so the agent is told what it is: without that it cannot tell a closed path from a wrong command
        return (
            f"Blocked by the risk monitor (event #{event_id}). {note} Do not try to get around this limit. "
            "If the task can be done within it, do that; otherwise tell the user."
        )
    # any other reason for a deny goes back with no rule detail
    return f"Blocked by the risk monitor (event #{event_id}). Do not retry or work around this; tell the user."


def pre_tool_output(verdict: str, event_id, reason: str, note: str = "") -> dict | None:
    """Print nothing on allow, so Claude Code's own permission flow runs as usual."""
    if verdict == "allow":
        return None
    if verdict == "ask":
        # the reason for an ask is shown only to the user, so it can carry detail
        decision, text = "ask", f"The risk monitor wants a person to confirm this (event #{event_id}): {reason}"
    else:
        decision, text = "deny", refusal(verdict, event_id, note)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": text}}


def own_wait(argv: list[str]) -> int:
    """How long this machine's hook may pause a run for a reviewer, as `riskmon init --wait-for-review` wrote it into the hook command."""
    if "--wait" in argv[:-1] and argv[argv.index("--wait") + 1].isdigit():
        return int(argv[argv.index("--wait") + 1])
    return 0


def wait_for_review(payload: dict, event_id, seconds: int, port: int) -> str:
    """Pause until a person decides the held action or the time is up. Returns `false_positive` (approved), `dismissed`, or `` for no decision."""
    end = time.time() + seconds
    while (left := end - time.time()) > 0:
        asked = time.time()
        try:
            got = ask_daemon({"hook_event_name": "ReviewWait", "session_id": payload.get("session_id"), "event_id": event_id, "riskmon_left": left}, port)
        except Exception:
            return ""  # the daemon went away; the action stays held
        if got.get("review"):
            return str(got["review"])
        if time.time() - asked < 1:
            time.sleep(min(1, max(0, end - time.time())))  # an answer that came straight back must not turn this into a busy loop
    return ""


def fallback(payload: dict, cfg: dict) -> dict | None:
    """Daemon unavailable: run L0 locally and deny anything over the gray threshold."""
    l0 = rules.assess(from_claude_code(payload), cfg)
    if l0.top >= cfg["thresholds"]["gray"]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "The risk monitor is unavailable, so this action is blocked. Tell the user.",
            }
        }
    return None


def main() -> None:
    payload = json.load(sys.stdin)
    cfg = config.load()
    name = payload.get("hook_event_name")
    out = None
    # the launcher of an unattended run sets these; the hook inherits them and tells the daemon
    payload["riskmon_mode"] = os.environ.get("RISKMON_MODE", "")
    payload["riskmon_observe"] = os.environ.get("RISKMON_OBSERVE") == "1"
    try:
        payload["riskmon_scope"] = json.loads(os.environ.get("RISKMON_SCOPE") or "{}")
    except ValueError:
        payload["riskmon_scope"] = {}
    # which project's policy applies follows the repository, or a name the launcher gave
    payload["riskmon_repo"], payload["riskmon_root"] = repo.find(str(payload.get("cwd") or os.getcwd()))
    payload["riskmon_project"] = os.environ.get("RISKMON_PROJECT", "")
    payload["riskmon_wait"] = own_wait(sys.argv)
    # a daemon on another machine cannot know these: `~` in its policy has to mean this user's home, and the files
    # it must keep an agent away from include this machine's copy of the monitor and its token
    payload["riskmon_home"], payload["riskmon_monitor"] = os.path.expanduser("~"), cfg["monitor_paths"]
    try:
        resp = ask_daemon(payload, cfg["port"])
        if name == "PreToolUse":
            verdict = resp.get("verdict", "allow")
            if verdict == "hold" and resp.get("wait"):
                # this machine is set up to pause an unattended run for a reviewer instead of moving on at once
                review = wait_for_review(payload, resp.get("event_id"), int(resp["wait"]), cfg["port"])
                verdict = {"false_positive": "allow", "dismissed": "reviewed"}.get(review, "hold")
            out = pre_tool_output(verdict, resp.get("event_id"), resp.get("reason", ""), resp.get("agent_note", ""))
        elif name == "UserPromptSubmit" and resp.get("suspended"):
            sid = payload.get("session_id")
            out = {
                "decision": "block",
                "reason": f"The risk monitor has suspended this session: {resp.get('reason')}\n"
                f"Review it, then resume it in the console or run: riskmon resume {sid}",
            }
    except Exception:
        if name == "PreToolUse":
            out = fallback(payload, cfg)
    if out:
        print(json.dumps(out, ensure_ascii=False))


def cli() -> None:
    try:
        main()
    except Exception as e:  # treat any unexpected error as a block: exit 2 is the only exit code that blocks on its own
        print(f"risk monitor hook failed: {e}", file=sys.stderr)
        sys.exit(2)
