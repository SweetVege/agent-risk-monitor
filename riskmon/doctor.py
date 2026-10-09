"""riskmon doctor: see at a glance which step of the setup is still missing."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

from . import config, install


def check(project: str = ".") -> bool:
    cfg = config.load()
    ok = True

    def line(good: bool | None, text: str, fix: str = "") -> None:
        nonlocal ok
        ok = ok and good is not False
        print(f"  {'✓' if good else '·' if good is None else '✗'} {text}" + (f"\n      {fix}" if fix and not good else ""))

    print("Environment")
    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "3.10 or later is required")
    user_policy = config.user_policy_path()
    line(None if not user_policy.exists() else True,
         f"policy: {user_policy}" if user_policy.exists() else f"policy: defaults (to change something, create {user_policy} with just the keys you change)")
    line(True, f"audit data: {config.data_dir()}")

    print("Daemon")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/healthz", timeout=3) as resp:
            health = json.load(resp)
        line(True, f"running on 127.0.0.1:{cfg['port']}")
    except OSError:
        health = None
        line(False, f"not running on 127.0.0.1:{cfg['port']}", "start it: riskmon serve")

    print(f"Project {Path(project).expanduser().resolve()}")
    have = install.installed_events(project)
    missing = [e for e in install.EVENTS if e not in have]
    line(not missing, "hooks installed" if not missing else f"hooks missing: {', '.join(missing)}", "install them: run riskmon init in the project")

    print("Unattended runs (optional)")
    cli = shutil.which("claude") or (str(Path.home() / ".local/bin/claude") if (Path.home() / ".local/bin/claude").exists() else "")
    line(None if not cli else True, f"claude CLI: {cli}" if cli else "claude CLI not found; riskmon run will not work")
    return ok
