"""Policies in two layers: a baseline every session follows, and what each project adds to it.

A project is a repository, a directory, or both. A session started in a checkout of that repository,
or in that directory or under it, gets the baseline plus that project's own scope: hosts it may contact, directories it may use, paths that are read-only or
protected there. A project can also make the handling of a kind of action stricter than the
baseline. It cannot make it looser, so the baseline stays the floor for everything.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config, repo as repos
from .scope import RULES

RANK = {"score": 0, "confirm": 1, "deny": 2}
MODES = ("unattended", "attended")
LISTS = ("allow_hosts", "extra_dirs", "read_only", "protected_paths")
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")
_NAME = re.compile(r"^[\w][\w .-]{0,59}$")
MAX_ITEMS = 200


def path() -> Path:
    return config.home() / "projects.json"


def clean_list(kind: str, values) -> list[str]:
    """A list of hosts or paths as a person typed them, checked and with duplicates dropped. Raises ValueError with a reason."""
    if not isinstance(values, list) or len(values) > MAX_ITEMS:
        raise ValueError(f"{kind} must be a list of at most {MAX_ITEMS} entries")
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip() or len(v) > 400 or "\n" in v or "\x00" in v:
            raise ValueError(f"{kind}: {v!r} is not a usable entry")
        v = v.strip()
        if kind == "allow_hosts" and not _HOST.match(v):
            raise ValueError(f"{v!r} is not a host name; give it without a scheme or path, such as api.example.org")
        if v not in out:
            out.append(v)
    return out


def clean_handling(rules) -> dict:
    if not isinstance(rules, dict):
        raise ValueError("handling must be an object")
    for rule, how in rules.items():
        if rule not in RULES or how not in RANK:
            raise ValueError(f"unknown handling {rule!r}: {how!r}")
    return {rule: how for rule, how in rules.items() if how != "score"}


def clean_project(body: dict) -> dict:
    name, root = body.get("name"), body.get("root")
    if not isinstance(name, str) or not _NAME.match(name.strip()):
        raise ValueError("a project needs a name of letters, digits, spaces, dots or dashes")
    root = root.strip() if isinstance(root, str) else ""
    given = body.get("repo")
    given = given.strip() if isinstance(given, str) else ""
    # a repository is kept the way a session's is reported, whether it was typed as a clone address or as host/owner/name
    repo = repos.normalize(given) or repos.normalize("https://" + given) if given else ""
    if given and (not repo or repo.count("/") < 2):
        raise ValueError(f"{given!r} is not a repository; give its clone address, or host/owner/name such as github.com/acme/billing")
    if root and not os.path.isabs(os.path.expanduser(root)):
        raise ValueError("a project's directory is given as a full path")
    if not root and not repo:
        raise ValueError("a project needs a repository, the full path of a directory, or both")
    project = {"name": name.strip(), "repo": repo, "root": os.path.normpath(os.path.expanduser(root)) if root else ""}
    for kind in LISTS:
        project[kind] = clean_list(kind, body.get(kind, []))
    for mode in MODES:
        project[mode] = clean_handling(body.get(mode, {}))
    return project


def load() -> list[dict]:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for p in raw.get("projects", []) if isinstance(raw, dict) else []:
        try:
            out.append(clean_project(p))
        except ValueError:
            continue  # an entry edited by hand into something unusable is skipped, not allowed to stop the daemon
    return out


def save(projects: list[dict]) -> None:
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find(projects: list[dict], cwd: str) -> dict | None:
    """The project whose directory holds `cwd`; the innermost one when projects are nested."""
    here = os.path.normpath(cwd or "/")
    best = None
    for p in projects:
        if p["root"] and (here == p["root"] or here.startswith(p["root"].rstrip("/") + "/")):
            if best is None or len(p["root"]) > len(best["root"]):
                best = p
    return best


def match(projects: list[dict], cwd: str, repo: str = "", repo_root: str = "", named: str = "") -> tuple[dict | None, str]:
    """The project a session belongs to, and the directory its relative paths are taken from on that session's machine.

    A name given by whoever launched the run comes first. Then the repository, which is the same on every machine.
    Then the directory, which is how a project with no remote is found.
    """
    here = os.path.normpath(cwd or "/")
    if named:
        chosen = next((p for p in projects if p["name"] == named), None)
        if chosen:
            return chosen, repo_root or here
    if repo:
        chosen = next((p for p in projects if p["repo"] == repo), None)
        if chosen:
            return chosen, repo_root or here
    chosen = find(projects, cwd)
    return (chosen, chosen["root"]) if chosen else (None, "")


def apply(cfg: dict, project: dict | None, root: str = "") -> dict:
    """The baseline with one project's additions. The project widens what is in scope there and can only tighten the handling.

    `root` is where the project is for the session at hand; a project found by its repository has no directory of its own.
    """
    if not project:
        return cfg
    root = root or project["root"] or "/"

    def under_root(p: str) -> str:
        p = os.path.expanduser(p)
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))

    scope = dict(cfg["scope"])
    scope["extra_dirs"] = scope["extra_dirs"] + [under_root(p) for p in project["extra_dirs"]]
    scope["read_only"] = scope.get("read_only", []) + [under_root(p) for p in project["read_only"]]
    for mode in MODES:
        merged = dict(scope.get(mode, {}))
        for rule, how in project[mode].items():
            if RANK[how] > RANK[merged.get(rule, "score")]:
                merged[rule] = how
        scope[mode] = merged
    return {**cfg, "allow_hosts": cfg["allow_hosts"] + project["allow_hosts"], "protected_paths": cfg["protected_paths"] + [under_root(p) for p in project["protected_paths"]], "scope": scope}


def save_baseline(policy_file: str | os.PathLike, body: dict) -> None:
    """Write the parts of the baseline the console edits into the policy file, leaving every other key in it alone."""
    target = Path(policy_file)
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    scope = dict(current.get("scope", {}))
    if "allow_hosts" in body:
        current["allow_hosts"] = clean_list("allow_hosts", body["allow_hosts"])
    if "protected_paths" in body:
        current["protected_paths"] = clean_list("protected_paths", body["protected_paths"])
    if "read_only" in body:
        scope["read_only"] = clean_list("read_only", body["read_only"])
    for mode in MODES:
        if mode in body:
            # every rule is written, so that choosing "score" here overrides a stricter default instead of falling back to it
            chosen = clean_handling(body[mode])
            scope[mode] = {rule: chosen.get(rule, "score") for rule in RULES}
    if scope:
        current["scope"] = scope
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
