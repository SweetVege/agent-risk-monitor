"""Activity report: what an agent actually touched during a session.

Scoring answers whether one action should run. The report answers where the run reached overall:
files outside the workspace, hosts contacted, programs run, how widely it searched, and the
actions that were reviewed or stopped. After an unattended run this is the first thing a person
looks at.
"""
from __future__ import annotations

import os
from collections import Counter

from . import rules
from .events import ActionEvent
from .optional import HAS_DRIFT, drift
from .store import Store

_SENSITIVE = {"protected_access": "protected path", "monitor_probe": "monitor configuration", "monitor_tamper": "the monitor itself"}


def build(store: Store, session_id: str, cfg: dict, workspace: str = "") -> dict:
    """`workspace` is only needed for old records: early events did not store their working directory, and without it inside and outside cannot be told apart."""
    events = list(reversed(store.events(session_id, limit=5000)))
    st = store.load_state(session_id)
    if st.scope_hosts or st.scope_dirs:  # judge against the scope this run declared, not the default policy
        cfg = {**cfg, "allow_hosts": cfg["allow_hosts"] + st.scope_hosts,
               "scope": {**cfg["scope"], "extra_dirs": cfg["scope"]["extra_dirs"] + st.scope_dirs}}
    verdicts = Counter(e["verdict"] for e in events)
    cwd = workspace or (Counter(e["detail"].get("cwd", "") for e in events).most_common(1)[0][0] if events else "")

    programs, unknown = Counter(), Counter()
    outside_files, sensitive, hosts, searches, attention = [], [], {}, [], []
    written_inside = read_inside = failed = 0

    for e in events:
        d = e["detail"]
        ev_cwd = d.get("cwd") or cwd
        # re-read the action with the current rules, so old records get a report too
        l0 = rules.assess(ActionEvent(session_id, e["tool"], e["kind"], e["target"], cwd=ev_cwd), cfg)
        ran = e["verdict"] == "allow"
        failed += (d.get("outcome") or {}).get("status") == "failed"

        if e["kind"] == "shell":
            for name, _, _ in rules._programs(e["target"])[0]:
                programs[name] += 1
            unknown.update(l0.unknown)
        elif e["kind"] in ("file_read", "file_write") and e["target"]:
            inside = not (l0.facets & {"outside_workspace", "system_write", "protected_access", "monitor_probe", "monitor_tamper"})
            if inside:
                written_inside += e["kind"] == "file_write" and ran
                read_inside += e["kind"] == "file_read" and ran
            elif not l0.facets & set(_SENSITIVE):  # sensitive targets are listed separately, not repeated here
                outside_files.append({"path": e["target"], "write": e["kind"] == "file_write", "ran": ran, "event": e["id"]})

        for facet, label in _SENSITIVE.items():
            if facet in l0.facets:
                sensitive.append({"what": label, "target": e["target"][:200], "ran": ran, "verdict": e["verdict"], "event": e["id"]})
        for h in l0.hosts:
            entry = hosts.setdefault(h, {"host": h, "allowed": rules._host_allowed(h, cfg), "attempts": 0, "ran": 0})
            entry["attempts"] += 1
            entry["ran"] += ran
        for scope_text in l0.searches:
            searches.append({"scope": scope_text, "ran": ran, "event": e["id"]})
        if e["verdict"] != "allow" or e["tier"] in ("L2", "HUMAN") or d.get("would"):
            reason = "; ".join(d.get("combos", []) + d.get("findings", []) + ([f"judge: {d['rationale']}"] if d.get("rationale") else []))
            attention.append({"event": e["id"], "verdict": e["verdict"], "score": e["score"], "tier": e["tier"],
                              "action": f"{e['tool']}: {e['target'][:160]}", "reason": reason, "review": d.get("review", ""),
                              "would": d.get("would", "")})

    for h in st.hosts:  # hosts reached indirectly by a script the agent ran are only in the session state
        hosts.setdefault(h, {"host": h, "allowed": rules._host_allowed(h, cfg), "attempts": 0, "ran": 0, "indirect": True})

    return {
        "session_id": session_id,
        "unattended": st.unattended,
        "observe": st.observe,
        "suspended": st.suspended,
        "suspended_reason": st.suspended_reason,
        "tasks": st.tasks,
        "declared_scope": {"hosts": st.scope_hosts, "dirs": st.scope_dirs},
        "workspace": cwd,
        "started": events[0]["ts"] if events else None,
        "ended": events[-1]["ts"] if events else None,
        "actions": len(events),
        "verdicts": dict(verdicts),
        "failed_actions": failed,
        "files": {"read_inside": read_inside, "written_inside": written_inside, "outside": outside_files},
        "sensitive": sensitive,
        "drift": {"available": HAS_DRIFT, "now": st.drift, "peak": st.drift_peak, "peak_level": drift.level(st.drift_peak, cfg),
                  "trajectory": [e["detail"]["drift"] for e in events if "drift" in e["detail"]]},
        "regions": st.regions,
        "searches": searches,
        "hosts": sorted(hosts.values(), key=lambda h: (h["allowed"], h["host"])),
        "programs": dict(programs.most_common()),
        "unknown_programs": dict(unknown),
        "attention": attention,
    }


_VERDICT = {"allow": "allowed", "ask": "confirm", "hold": "held", "deny": "denied", "suspend": "suspended"}


def render(r: dict) -> str:
    """The plain-text version, for a person."""
    did = lambda x: "" if x.get("ran") else " (did not run)"  # noqa: E731
    counts = ", ".join(f"{_VERDICT[k]} {v}" for k, v in r["verdicts"].items()) or "none"
    scope = r["declared_scope"]
    out = [
        f"Session {r['session_id']}" + (" (unattended)" if r["unattended"] else "")
        + (" (observe mode: held actions were let through)" if r.get("observe") else "")
        + ("  SUSPENDED: " + r["suspended_reason"] if r["suspended"] else ""),
        f"Workspace: {r['workspace'] or 'unknown'}",
        *([f"Allowed at launch: hosts {', '.join(scope['hosts']) or 'none'}; directories {', '.join(scope['dirs']) or 'none'}"]
          if scope["hosts"] or scope["dirs"] else []),
        f"Actions: {r['actions']} ({counts}); {r['failed_actions']} failed when run",
        f"Inside the workspace: {r['files']['read_inside']} reads, {r['files']['written_inside']} writes",
    ]
    dr = r["drift"]
    if dr["peak"]:
        out.append(f"Drift: peak {dr['peak']:g} ({dr['peak_level']}), {dr['now']:g} at the end. Step by step: " + " ".join(f"{v:g}" for v in dr["trajectory"]))
    if r["regions"]:
        out.append("Reached outside the workspace, in order: " + " → ".join(r["regions"]))
    for f in r["files"]["outside"]:
        out.append(f"  {'wrote' if f['write'] else 'read'} {f['path']}{did(f)}")
    for s in r["searches"]:
        out.append(f"  {s['scope']}{did(s)}")
    if r["sensitive"]:
        out.append("Sensitive targets:")
        out += [f"  {s['what']}: {s['target']}{did(s)}" for s in r["sensitive"]]
    if r["hosts"]:
        out.append("Network:")
        for h in r["hosts"]:
            how = "reached indirectly by a script it ran" if h.get("indirect") else f"{h['attempts']} attempted, {h['ran']} run"
            out.append(f"  {h['host']} ({'allowlisted' if h['allowed'] else 'not allowlisted'}; {how})")
    if r["unknown_programs"]:
        out.append("Programs outside the known list: " + ", ".join(f"{k} ×{v}" for k, v in r["unknown_programs"].items()))
    if r["attention"]:
        out.append("Reviewed or stopped:")
        for a in r["attention"]:
            mark = (", overturned by a person" if a["review"] else "") + (", would have been held" if a.get("would") else "")
            out.append(f"  #{a['event']} {_VERDICT[a['verdict']]} (score {a['score']}, {a['tier']}{mark}) {a['action']}")
            out.append(f"      {a['reason']}")
    return "\n".join(out)
