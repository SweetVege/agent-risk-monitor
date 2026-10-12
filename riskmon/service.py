"""Keep the daemon running: started at login and restarted if it stops.

Protection that covers every session is only as good as the daemon being up. On macOS this registers
it with launchd as an agent of the current user. Nothing here needs administrator rights, and no
credential is written into the service definition.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import config

LABEL = "com.riskmon.daemon"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def definition(policy: str | None = None) -> dict:
    """What launchd is told to run. Environment carries where the monitor's home is and nothing secret."""
    args = [sys.executable, "-u", "-m", "riskmon"] + (["--policy", str(Path(policy).expanduser().resolve())] if policy else []) + ["serve"]
    log = str(config.data_dir() / "daemon.log")
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"}
    if os.environ.get("RISKMON_HOME"):
        env["RISKMON_HOME"] = os.environ["RISKMON_HOME"]
    spec = {
        "Label": LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,  # if it cannot start, try again every ten seconds, not in a tight loop
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": env,
    }
    if config.in_source_tree():
        spec["WorkingDirectory"] = str(config.SOURCE)  # run from a checkout, the package is found from there
    return spec


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def supported() -> bool:
    return sys.platform == "darwin"


def running() -> bool | None:
    """Whether launchd has the daemon loaded. None where this is not supported."""
    if not supported():
        return None
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def install(policy: str | None = None) -> str:
    if not supported():
        raise RuntimeError("starting at login is only set up for macOS so far; elsewhere, run `riskmon serve` from your own service manager")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.data_dir().mkdir(parents=True, exist_ok=True)
    if running():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    path.write_bytes(plistlib.dumps(definition(policy)))
    done = _launchctl("bootstrap", _domain(), str(path))
    if done.returncode != 0:
        raise RuntimeError(f"launchd would not load it: {done.stderr.strip() or done.stdout.strip()}")
    return str(path)


def remove() -> bool:
    if not supported():
        return False
    path, was = plist_path(), running()
    if was:
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    if path.exists():
        path.unlink()
        return True
    return bool(was)
