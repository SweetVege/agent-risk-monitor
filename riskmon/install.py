"""Install the hooks into a project's .claude/settings.json, or take them out.

Only our own entries are touched: other hooks, permission rules and settings stay as they are.
"""
from __future__ import annotations

import json
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


def hooks_block() -> dict:
    hook = {"type": "command", "command": hook_command(), "timeout": 20}
    return {
        event: [{"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}]
        for event, matcher in EVENTS.items()
    }


def _is_ours(group: dict) -> bool:
    return any(m in str(h.get("command", "")) for h in group.get("hooks", []) for m in MARKS)


def settings_path(project: str) -> Path:
    return Path(project).expanduser().resolve() / ".claude" / "settings.json"


def installed_events(project: str) -> list[str]:
    path = settings_path(project)
    if not path.exists():
        return []
    hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
    return [e for e in EVENTS if any(_is_ours(g) for g in hooks.get(e, []))]


def apply(project: str, remove: bool = False) -> tuple[Path, list[str]]:
    """Return (settings file, events changed). Running it again does not add duplicate entries."""
    path = settings_path(project)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    block, touched = hooks_block(), []
    for event in EVENTS:
        before = hooks.get(event, [])
        ours = [g for g in before if _is_ours(g)]
        current = [h.get("command") for g in ours for h in g.get("hooks", [])]
        if not remove and current == [hook_command()]:
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
