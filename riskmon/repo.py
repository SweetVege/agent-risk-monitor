"""Which repository a directory belongs to, read from its git files.

A project's policy follows the repository, so that the same code checked out at different paths on
different machines is one project. The hook runs this on every action, so it reads two or three small
files and starts no process.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!/)(.+)$")  # git@host:owner/name
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?([^:/\s]+)(?::\d+)?/(.+)$", re.I)  # scheme://user@host:port/owner/name
_SECTION = re.compile(r'^\s*\[\s*remote\s+"([^"]+)"\s*\]\s*$')
_KEY = re.compile(r"^\s*url\s*=\s*(.+?)\s*$")


def normalize(url: str) -> str:
    """A remote's address as `host/owner/name`: lower case, no scheme, user, port or `.git`. Empty if it is not a remote address.

    The same repository cloned over SSH and over HTTPS then reads the same, and a credential in the address is dropped.
    """
    url = (url or "").strip().strip('"')
    m = _URL.match(url) or _SCP.match(url)
    if not m:
        return ""
    path = re.sub(r"\.git$", "", m.group(2).strip("/"), flags=re.I)
    if not path or ".." in path.split("/"):
        return ""
    return f"{m.group(1)}/{path}".lower()[:300]


def _git_dir(start: str) -> tuple[Path, Path] | None:
    """The working tree that holds `start` and the directory its git config is in."""
    here = Path(os.path.abspath(start or "."))
    for d in (here, *here.parents):
        dot = d / ".git"
        if dot.is_dir():
            return d, dot
        if dot.is_file():  # a linked worktree or a submodule: the file says where the git directory is
            try:
                line = dot.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return None
            if not line.startswith("gitdir:"):
                return None
            git = (d / line[7:].strip()).resolve()
            try:  # a linked worktree keeps its config in the main checkout's git directory
                git = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
            except OSError:
                pass
            return d, git
    return None


def find(cwd: str) -> tuple[str, str]:
    """(`host/owner/name`, working tree root) for the repository `cwd` is in. Both empty when it is in none, or the repository has no remote."""
    found = _git_dir(cwd)
    if not found:
        return "", ""
    root, git = found
    try:
        lines = (git / "config").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    remotes, current = {}, None
    for line in lines:
        section = _SECTION.match(line)
        if section:
            current = section.group(1)
        elif line.lstrip().startswith("["):
            current = None
        elif current and current not in remotes:
            key = _KEY.match(line)
            if key:
                remotes[current] = key.group(1)
    # origin is what a clone is made from; with no origin, a single other remote is unambiguous
    url = remotes.get("origin") or (next(iter(remotes.values())) if len(remotes) == 1 else "")
    name = normalize(url)
    return (name, str(root)) if name else ("", "")
