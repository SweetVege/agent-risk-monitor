"""Claude Code hook: hand each action to the local riskmon daemon for scoring and turn its verdict into a Claude Code decision.

Claude Code lets a tool call through when a hook times out or crashes, so this keeps its own
timeout and its own fallback: with the daemon unavailable, out-of-bounds actions are denied
and the rest go through.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from . import config, rules
from .events import from_claude_code

HTTP_TIMEOUT = 12  # must stay below the hook timeout configured in settings.json


def ask_daemon(payload: dict, port: int) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/hook",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read())


def pre_tool_output(verdict: str, event_id, reason: str) -> dict | None:
    """Print nothing on allow, so Claude Code's own permission flow runs as usual."""
    if verdict == "allow":
        return None
    if verdict == "ask":
        # the reason for an ask is shown only to the user, so it can carry detail
        decision, text = "ask", f"The risk monitor wants a person to confirm this (event #{event_id}): {reason}"
    elif verdict == "hold":
        decision = "deny"
        text = (
            f"Held for human review by the risk monitor (event #{event_id}). Nobody is available to approve it "
            "right now. Do not retry it or look for another way to do the same thing. Carry on with the parts of "
            "the task that do not need it; if nothing is left, stop and report exactly what is waiting on review."
        )
    else:
        # the reason for a deny goes back to the model, so it carries no rule detail
        decision = "deny"
        text = f"Blocked by the risk monitor (event #{event_id}). Do not retry or work around this; tell the user."
        if verdict == "suspend":
            text = "This session is suspended by the risk monitor. Stop and tell the user to review it."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": text}}


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
    try:
        resp = ask_daemon(payload, cfg["port"])
        if name == "PreToolUse":
            out = pre_tool_output(resp.get("verdict", "allow"), resp.get("event_id"), resp.get("reason", ""))
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
