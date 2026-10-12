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


def _rows(store: Store, where: str, args: tuple, limit: int) -> list[dict]:
    with store._lock:
        rows = store._db.execute(
            f"SELECT e.*, s.state AS session_state FROM events e LEFT JOIN sessions s ON s.id = e.session_id WHERE {where} ORDER BY e.id DESC LIMIT ?", (*args, limit)
        ).fetchall()
    out = []
    for r in rows:
        detail = json.loads(r["detail"])
        state = json.loads(r["session_state"]) if r["session_state"] else {}
        tasks = state.get("tasks") or []
        out.append({
            "id": r["id"], "session_id": r["session_id"], "ts": r["ts"], "tool": r["tool"], "target": r["target"][:600], "verdict": r["verdict"], "score": r["score"], "tier": r["tier"],
            "would": detail.get("would", ""), "kind": kind_of(detail), "review": detail.get("review", ""),
            "reason": "; ".join(detail.get("combos", []) + detail.get("findings", [])), "judge": detail.get("rationale", ""),
            "task": tasks[-1][:200] if tasks else "", "unattended": bool(state.get("unattended")), "suspended": bool(state.get("suspended")),
        })
    return out


def incidents(store: Store, limit: int = 100) -> list[dict]:
    """Actions that did not simply go through, newest first."""
    marks = ",".join("?" * len(STOPPED))
    return _rows(store, f"e.verdict IN ({marks}) OR json_extract(e.detail, '$.would') != ''", STOPPED, limit)


def pending(store: Store, limit: int = 100) -> list[dict]:
    """Held actions nobody has looked at yet, and sessions stopped until someone resumes them."""
    held = _rows(store, "e.verdict = 'hold' AND coalesce(json_extract(e.detail, '$.review'), '') = ''", (), limit)
    return held


def overview(store: Store, now: float | None = None) -> dict:
    now = now or time.time()
    day, week = now - 86400, now - 7 * 86400
    with store._lock:
        counts = {
            "sessions_day": store._db.execute("SELECT count(*) FROM sessions WHERE updated >= ?", (day,)).fetchone()[0],
            "sessions_week": store._db.execute("SELECT count(*) FROM sessions WHERE updated >= ?", (week,)).fetchone()[0],
            "sessions_all": store._db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "actions_week": store._db.execute("SELECT count(*) FROM events WHERE ts >= ?", (week,)).fetchone()[0],
            "actions_all": store._db.execute("SELECT count(*) FROM events").fetchone()[0],
        }
        recent = store._db.execute("SELECT verdict, detail FROM events WHERE ts >= ? AND verdict != 'allow'", (week,)).fetchall()
        suspended = [r["id"] for r in store._db.execute("SELECT id, state FROM sessions").fetchall() if json.loads(r["state"]).get("suspended")]
    by_verdict, by_kind = Counter(), Counter()
    for r in recent:
        by_verdict[r["verdict"]] += 1
        by_kind[kind_of(json.loads(r["detail"]))] += 1
    return {**counts, "stopped_week": dict(by_verdict), "kinds_week": by_kind.most_common(8), "pending": len(pending(store)), "suspended": suspended}
