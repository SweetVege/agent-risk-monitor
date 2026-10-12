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

    from .hook import client

    joined = client()["server"]
    if joined:
        print("Team daemon")
        try:
            with urllib.request.urlopen(joined + "/healthz", timeout=5) as resp:
                json.load(resp)
            line(True, f"this machine's sessions are checked at {joined}, which is answering")
        except OSError as e:
            line(False, f"this machine's sessions are checked at {joined}, which is not answering ({e})",
                 "until it does, ordinary work goes through and anything beyond the default policy is refused")
    print("Daemon on this machine" if joined else "Daemon")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/healthz", timeout=3) as resp:
            health = json.load(resp)
        line(True, f"running on 127.0.0.1:{cfg['port']}")
    except OSError:
        health = None
        line(None if joined else False, f"not running on 127.0.0.1:{cfg['port']}" + (" (not needed: this machine has joined a team's daemon)" if joined else ""),
             "" if joined else "start it: riskmon serve, or keep it running with: riskmon service install")
    from . import service

    kept = service.running()
    if kept is not None:
        line(True if kept else None, "starts at login and is restarted if it stops" if kept else "not kept running: it stops when its terminal closes or the machine restarts",
             "to keep it running: riskmon service install")

    print("Protection")
    everywhere = [e for e in install.EVENTS if e not in install.installed_events(user=True)]
    have = install.installed_events(project)
    missing = [e for e in install.EVENTS if e not in have]
    if not everywhere:
        line(True, f"on for every session this user starts ({install.user_settings_path()})")
    else:
        line(None, "not on for the whole machine" + (f"; partly installed, missing {', '.join(everywhere)}" if len(everywhere) < len(install.EVENTS) else ""),
             "to cover every session: riskmon init --user")
        line(not missing, f"on for the project {Path(project).expanduser().resolve()}" if not missing else f"off for the project {Path(project).expanduser().resolve()}"
             + (f": missing {', '.join(missing)}" if have else ""), "to cover this project only: riskmon init")

    print("Unattended runs (optional)")
    cli = shutil.which("claude") or (str(Path.home() / ".local/bin/claude") if (Path.home() / ".local/bin/claude").exists() else "")
    line(None if not cli else True, f"claude CLI: {cli}" if cli else "claude CLI not found; riskmon run will not work")
    return ok
