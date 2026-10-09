"""Adapter layer: turn each agent's actions into an ActionEvent.

Supporting another agent means writing one more from_xxx function; the scoring tiers do not change.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

KINDS = ("shell", "network", "file_read", "file_write", "agent", "other")


@dataclass
class ActionEvent:
    session_id: str
    tool: str
    kind: str
    target: str  # command, path or URL
    content: str = ""  # accompanying text, such as the content being written to a file
    cwd: str = ""
    source: str = "claude-code"
    ts: float = field(default_factory=time.time)
    tool_use_id: str = ""  # ties the outcome back to this scored action
    transcript_path: str = ""
    described: str = ""  # the model's own one-line description of the call (Bash's description parameter)
    unattended: bool = False  # nobody is watching this run, so nobody can answer a confirmation prompt
    observe: bool = False  # observe mode: let would-be holds through and record them, to see what the agent does when nothing stops it
    scope_hosts: list = field(default_factory=list)  # extra hosts allowed by whoever launched this run
    scope_dirs: list = field(default_factory=list)  # extra directories allowed


_FILE_READ = {"Read", "Glob", "Grep", "NotebookRead"}
_FILE_WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass
class JudgeResult:
    """What a judge returns for one action: a 0-100 score per dimension and a one-line reason."""

    scores: dict  # deviation, boundary, stealth, resource
    rationale: str
    input_tokens: int = 0
    output_tokens: int = 0


def from_claude_code(payload: dict) -> ActionEvent:
    """The stdin JSON of a Claude Code PreToolUse hook → ActionEvent."""
    tool = str(payload.get("tool_name", ""))
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        inp = {"value": inp}
    kind, target, content = "other", "", ""

    if tool == "Bash":
        kind, target = "shell", str(inp.get("command", ""))
    elif tool in _FILE_READ:
        kind = "file_read"
        target = str(inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or "")
        content = str(inp.get("pattern", ""))
    elif tool in _FILE_WRITE:
        kind = "file_write"
        target = str(inp.get("file_path") or inp.get("notebook_path") or "")
        edits = inp.get("edits") or []
        content = "\n".join(
            str(x)
            for x in [inp.get("content"), inp.get("new_string"), inp.get("new_source")]
            + [e.get("new_string") for e in edits if isinstance(e, dict)]
            if x
        )
    elif tool == "WebFetch":
        kind, target = "network", str(inp.get("url", ""))
    elif tool == "WebSearch":
        kind, target = "network", "search://" + str(inp.get("query", ""))
    elif tool in ("Agent", "Task"):
        kind, target = "agent", str(inp.get("prompt", ""))
    else:
        target = json.dumps(inp, ensure_ascii=False, sort_keys=True)

    scope = payload.get("riskmon_scope")
    scope = scope if isinstance(scope, dict) else {}
    return ActionEvent(
        session_id=str(payload.get("session_id", "unknown")),
        tool=tool,
        kind=kind,
        target=target,
        content=content,
        cwd=str(payload.get("cwd", "")),
        tool_use_id=str(payload.get("tool_use_id", "")),
        transcript_path=str(payload.get("transcript_path", "")),
        described=str(inp.get("description", ""))[:300],
        unattended=payload.get("riskmon_mode") == "unattended",
        observe=payload.get("riskmon_observe") is True,
        scope_hosts=[str(h) for h in scope.get("hosts", [])][:50],
        scope_dirs=[os.path.expanduser(str(d)) for d in scope.get("dirs", [])][:50],
    )
