"""Who may talk to a shared daemon, and as what.

There are two kinds of token and they do different things. A machine token is for a machine that
runs agents: it can hand in actions and get verdicts back, and nothing else. A member token is
for a person: it opens the console, and by role lets them review held actions or change the
policy. The two are never interchangeable. A machine that runs an agent holds its own token, so
if that token could approve an action, the agent could approve its own.

Only a hash of each token is kept. A token is shown once, when it is made.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

ROLES = ("admin", "reviewer")  # an admin can also do what a reviewer can
_NAME = re.compile(r"^[\w][\w .@-]{0,59}$")


@dataclass(frozen=True)
class Caller:
    kind: str  # "machine" or "member"
    name: str
    role: str = ""  # for a member: admin or reviewer

    @property
    def can_review(self) -> bool:
        return self.kind == "member"

    @property
    def can_administer(self) -> bool:
        return self.kind == "member" and self.role == "admin"


LOCAL = Caller("member", "local", "admin")  # on a daemon that only this machine can reach, the person at it


def path() -> Path:
    return config.home() / "access.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"machines": list(data.get("machines", [])), "members": list(data.get("members", []))}


def _save(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)


def add(kind: str, name: str, role: str = "") -> str:
    """Create a machine or a member and return its token, which is not stored and cannot be shown again."""
    if kind not in ("machine", "member"):
        raise ValueError("kind must be machine or member")
    if not isinstance(name, str) or not _NAME.match(name.strip()):
        raise ValueError("a name needs letters or digits, and may have spaces, dots, dashes or @")
    if kind == "member" and role not in ROLES:
        raise ValueError(f"a member's role is one of: {', '.join(ROLES)}")
    data, name = load(), name.strip()
    if any(e["name"] == name for e in data["machines"] + data["members"]):
        raise ValueError(f"{name!r} already exists; remove it first to issue a new token")
    token = ("rmm_" if kind == "machine" else "rmp_") + secrets.token_urlsafe(32)
    entry = {"name": name, "token_sha256": _digest(token), "created": int(time.time())}
    if kind == "member":
        entry["role"] = role
    data["machines" if kind == "machine" else "members"].append(entry)
    _save(data)
    return token


def remove(name: str) -> bool:
    data = load()
    kept = {k: [e for e in v if e["name"] != name] for k, v in data.items()}
    if kept == data:
        return False
    _save(kept)
    return True


def in_use() -> bool:
    data = load()
    return bool(data["machines"] or data["members"])


def identify(token: str, data: dict | None = None) -> Caller | None:
    """Whose token this is, or None. Every stored hash is compared, in constant time, so timing says nothing about which matched."""
    if not token:
        return None
    data = data if data is not None else load()
    digest, found = _digest(token), None
    for kind, entries in (("machine", data["machines"]), ("member", data["members"])):
        for e in entries:
            if hmac.compare_digest(e.get("token_sha256", ""), digest):
                found = Caller(kind, e["name"], e.get("role", "") if kind == "member" else "")
    return found
