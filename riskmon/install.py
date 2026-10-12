"""Install the hooks into Claude Code's settings, or take them out.

For the whole machine they go into the user's settings, so that every session is covered wherever it is
started and nothing has to be done per project. For one project they go into its .claude/settings.json.
Only our own entries are touched: other hooks, permission rules and settings stay as they are.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from importlib import metadata
from pathlib import Path

from . import config

EVENTS = {"PreToolUse": "*", "PostToolUse": "*", "PostToolUseFailure": "*", "UserPromptSubmit": None}
MARKS = ("riskmon hook", "claude_code_hook.py")  # each install style's hook command contains one of these; that is how we recognise our own entries


def hook_command() -> str:
    """`python -m riskmon hook` when the package is installed, the script in the checkout when run from source."""
    python = shlex.quote(sys.executable)
    try:
        metadata.version("agent-risk-monitor")
    except metadata.PackageNotFoundError:
        return f"{python} {shlex.quote(str(config.SOURCE / 'hooks' / 'claude_code_hook.py'))}"
    return f"{python} -m riskmon hook"


MAX_WAIT = 3600


def hooks_block(wait: int = 0) -> dict:
    """`wait` is how long, in seconds, the hook may pause an unattended run for a reviewer's decision on a held action.

    Claude Code lets an action through when its hook runs out of time, so the time allowed is always longer than
    anything the hook itself waits for.
    """
    wait = max(0, min(int(wait), MAX_WAIT))
    block = {}
    for event, matcher in EVENTS.items():
        waits = wait if event == "PreToolUse" else 0
        hook = {"type": "command", "command": hook_command() + (f" --wait {waits}" if waits else ""), "timeout": 20 + (waits + 10 if waits else 0)}
        block[event] = [{"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}]
    return block


def installed_wait(project: str = ".", user: bool = False) -> int:
    """How long the installed hook waits for a reviewer, in seconds; 0 when it does not."""
    path = settings_path(project, user)
    if not path.exists():
        return 0
    for group in json.loads(path.read_text(encoding="utf-8")).get("hooks", {}).get("PreToolUse", []):
        for h in group.get("hooks", []) if _is_ours(group) else []:
            words = str(h.get("command", "")).split()
            if "--wait" in words[:-1] and words[words.index("--wait") + 1].isdigit():
                return int(words[words.index("--wait") + 1])
    return 0


def _is_ours(group: dict) -> bool:
    return any(m in str(h.get("command", "")) for h in group.get("hooks", []) for m in MARKS)


def user_settings_path() -> Path:
    """Claude Code's settings for this user, which apply to every session on the machine."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"


def settings_path(project: str, user: bool = False) -> Path:
    return user_settings_path() if user else Path(project).expanduser().resolve() / ".claude" / "settings.json"


def installed_events(project: str = ".", user: bool = False) -> list[str]:
    path = settings_path(project, user)
    if not path.exists():
        return []
    hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
    return [e for e in EVENTS if any(_is_ours(g) for g in hooks.get(e, []))]


def apply(project: str = ".", remove: bool = False, user: bool = False, wait: int = 0) -> tuple[Path, list[str]]:
    """Return (settings file, events changed). Running it again does not add duplicate entries."""
    path = settings_path(project, user)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    block, touched = hooks_block(wait), []
    for event in EVENTS:
        before = hooks.get(event, [])
        ours = [g for g in before if _is_ours(g)]
        current = [h.get("command") for g in ours for h in g.get("hooks", [])]
        if not remove and current == [block[event][0]["hooks"][0]["command"]]:
            continue  # the same command is already installed; leave other fields (a user's statusMessage, say) alone
        after = [g for g in before if not _is_ours(g)] + ([] if remove else block[event])
        if after != before:
            touched.append(event)
        if after:
            hooks[event] = after
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    if touched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, touched
