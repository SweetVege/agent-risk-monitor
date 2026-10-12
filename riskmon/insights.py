"""What the console shows across sessions: what is waiting for a person, what was stopped, and what kind of thing it was."""
from __future__ import annotations

import json
import time
from collections import Counter

from .store import Store

# behaviour tag → the kind of limit an action met, most specific first
KINDS = (
    ("monitor_tamper", "The monitor itself"),
    ("monitor_probe", "The monitor itself"),
    ("readonly_write", "A read-only path"),
    ("protected_access", "A protected path"),
    ("egress", "A host off the allowlist"),
    ("internal_address", "A private network address"),
    ("outside_write", "Writing outside the workspace"),
    ("whole_search", "Searching the whole machine"),
    ("outside_search", "Searching outside the workspace"),
    ("outside_workspace", "A path outside the workspace"),
    ("piped_exec", "Running downloaded code"),
    ("privilege", "Elevated privileges"),
)
STOPPED = ("deny", "suspend", "hold", "ask")


def kind_of(detail: dict) -> str:
    facets = set(detail.get("facets") or [])
    for facet, label in KINDS:
        if facet in facets:
            return label
    return "Off-task or risky in context" if detail.get("rationale") else "Risk score"


def only(machine: str = "", project: str = "") -> tuple[str, tuple]:
    """A condition on the session `s` that narrows a query to one machine, one project, or both. `-` asks for sessions with none."""
    sql, args = "", ()
    for field, value in (("machine", machine), ("project", project)):
        if value:
            sql += f" AND coalesce(json_extract(s.state, '$.{field}'), '') = ?"
            args += ("" if value == "-" else value,)
    return sql, args


def _rows(store: Store, where: str, args: tuple, limit: int, scope: tuple = ("", ())) -> list[dict]:
    with store._lock:
        rows = store._db.execute(
            f"SELECT e.*, s.state AS session_state FROM events e LEFT JOIN sessions s ON s.id = e.session_id WHERE ({where}){scope[0]} ORDER BY e.id DESC LIMIT ?", (*args, *scope[1], limit)
        ).fetchall()
    out = []
    for r in rows:
        detail = json.loads(r["detail"])
        state = json.loads(r["session_state"]) if r["session_state"] else {}
        tasks = state.get("tasks") or []
        out.append({
            "id": r["id"], "session_id": r["session_id"], "ts": r["ts"], "tool": r["tool"], "target": r["target"][:600], "verdict": r["verdict"], "score": r["score"], "tier": r["tier"],
            "would": detail.get("would", ""), "kind": kind_of(detail), "review": detail.get("review", ""), "review_by": detail.get("review_by", ""), "wait_until": detail.get("wait_until", 0),
            "machine": state.get("machine", ""), "user": state.get("user", ""), "project": state.get("project", ""),
            "reason": "; ".join(detail.get("combos", []) + detail.get("findings", [])), "judge": detail.get("rationale", ""),
            "task": tasks[-1][:200] if tasks else "", "unattended": bool(state.get("unattended")), "suspended": bool(state.get("suspended")),
        })
    return out


def incidents(store: Store, limit: int = 100, machine: str = "", project: str = "") -> list[dict]:
    """Actions that did not simply go through, newest first."""
    marks = ",".join("?" * len(STOPPED))
    return _rows(store, f"e.verdict IN ({marks}) OR json_extract(e.detail, '$.would') != ''", STOPPED, limit, only(machine, project))


def pending(store: Store, limit: int = 100, machine: str = "", project: str = "") -> list[dict]:
    """Held actions nobody has looked at yet, and sessions stopped until someone resumes them."""
    held = _rows(store, "e.verdict = 'hold' AND coalesce(json_extract(e.detail, '$.review'), '') = ''", (), limit, only(machine, project))
    return held


def overview(store: Store, now: float | None = None, machine: str = "", project: str = "") -> dict:
    """Counts for the console's first page, for everything or for one machine or project. `pending` is always the whole queue: it is the number on the page's tab."""
    now = now or time.time()
    day, week = now - 86400, now - 7 * 86400
    scope, args = only(machine, project)
    events = "FROM events e LEFT JOIN sessions s ON s.id = e.session_id WHERE"
    with store._lock:
        one = lambda sql, *a: store._db.execute(sql, (*a, *args)).fetchone()[0]
        counts = {
            "sessions_day": one(f"SELECT count(*) FROM sessions s WHERE s.updated >= ?{scope}", day),
            "sessions_week": one(f"SELECT count(*) FROM sessions s WHERE s.updated >= ?{scope}", week),
            "sessions_all": one(f"SELECT count(*) FROM sessions s WHERE 1{scope}"),
            "actions_week": one(f"SELECT count(*) {events} e.ts >= ?{scope}", week),
            "actions_all": one(f"SELECT count(*) {events} 1{scope}"),
        }
        recent = store._db.execute(f"SELECT e.verdict, e.detail {events} e.ts >= ? AND e.verdict != 'allow'{scope}", (week, *args)).fetchall()
        states = [(r["id"], json.loads(r["state"])) for r in store._db.execute("SELECT id, state FROM sessions").fetchall()]
    by_verdict, by_kind = Counter(), Counter()
    for r in recent:
        by_verdict[r["verdict"]] += 1
        by_kind[kind_of(json.loads(r["detail"]))] += 1
    within = lambda st: (not machine or st.get("machine", "") == ("" if machine == "-" else machine)) and (not project or st.get("project", "") == ("" if project == "-" else project))
    return {
        **counts, "stopped_week": dict(by_verdict), "kinds_week": by_kind.most_common(8), "pending": len(pending(store)),
        "pending_here": len(pending(store, machine=machine, project=project)),
        "suspended": [sid for sid, st in states if st.get("suspended") and within(st)],
        # what there is to narrow the page to
        "machines": sorted({st.get("machine", "") for _, st in states} - {""}), "projects": sorted({st.get("project", "") for _, st in states} - {""}),
    }
