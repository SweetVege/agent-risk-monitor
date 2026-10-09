"""Policy and file locations.

The default policy ships with the package (default_policy.json). A user's policy.json holds
only the keys they change; everything else keeps its default, so upgrades need no manual merge.

The monitor's home directory holds the user policy and the audit data. It is, in order:
  1. the RISKMON_HOME environment variable
  2. the source checkout itself, when run from one (the development setup)
  3. ~/.riskmon
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
SOURCE = PACKAGE.parent
DEFAULT_POLICY = PACKAGE / "default_policy.json"


def in_source_tree() -> bool:
    return (SOURCE / "pyproject.toml").exists() and (SOURCE / "hooks").is_dir()


def home() -> Path:
    if os.environ.get("RISKMON_HOME"):
        return Path(os.environ["RISKMON_HOME"]).expanduser().resolve()
    return SOURCE if in_source_tree() else Path.home() / ".riskmon"


def user_policy_path() -> Path:
    return home() / "policy.json"


def data_dir() -> Path:
    return home() / "data"


def _merge(base: dict, override: dict) -> dict:
    """Merge dicts level by level; replace everything else, lists included, wholesale."""
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load(path: str | os.PathLike | None = None) -> dict:
    """The default policy plus the changes in `path` (default: policy.json in the home directory, which may not exist)."""
    cfg = json.loads(DEFAULT_POLICY.read_text(encoding="utf-8"))
    override = Path(path) if path else user_policy_path()
    if path or override.exists():
        cfg = _merge(cfg, json.loads(override.read_text(encoding="utf-8")))
    cfg["protected_paths"] = [os.path.expanduser(x) for x in cfg["protected_paths"]]
    cfg["scope"]["extra_dirs"] = [os.path.expanduser(x) for x in cfg["scope"]["extra_dirs"]]
    # where the monitor's own code, policy and audit data live; the self-protection rules need these
    cfg["monitor_paths"] = sorted({str(home()), str(SOURCE if in_source_tree() else PACKAGE)})
    return cfg
