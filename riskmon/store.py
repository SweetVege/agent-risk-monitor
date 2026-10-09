"""Session state and the audit log, in a single SQLite file."""
from __future__ import annotations

import json
import sqlite3
import threading

from .state import SessionState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, step INTEGER, ts REAL, tool TEXT, kind TEXT, target TEXT,
    verdict TEXT, score INTEGER, tier TEXT, latency_ms REAL, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def load_state(self, session_id: str) -> SessionState:
        with self._lock:
            row = self._db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
        return SessionState.from_dict(json.loads(row["state"])) if row else SessionState(session_id)

    def save_state(self, st: SessionState, ts: float) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions(id, state, updated) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated=excluded.updated",
                (st.session_id, json.dumps(st.to_dict(), ensure_ascii=False), ts),
            )

    def log_event(self, row: dict) -> int:
        cols = ("session_id", "step", "ts", "tool", "kind", "target", "verdict", "score", "tier", "latency_ms")
        with self._lock, self._db:
            cur = self._db.execute(
                f"INSERT INTO events({','.join(cols)},detail) VALUES({','.join('?' * len(cols))},?)",
                [row[c] for c in cols] + [json.dumps(row["detail"], ensure_ascii=False)],
            )
        return cur.lastrowid

    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def find_event(self, session_id: str, tool_use_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM events WHERE session_id=? AND json_extract(detail, '$.tool_use_id')=? ORDER BY id DESC LIMIT 1",
                (session_id, tool_use_id),
            ).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def set_event_detail(self, event_id: int, detail: dict) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE events SET detail=? WHERE id=?", (json.dumps(detail, ensure_ascii=False), event_id))

    def sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, state, updated FROM sessions ORDER BY updated DESC").fetchall()
        out = []
        for r in rows:
            st = json.loads(r["state"])
            out.append(
                {
                    "id": r["id"],
                    "updated": r["updated"],
                    "step": st["step"],
                    "denies": st["denies"],
                    "drift": st.get("drift", 0),
                    "drift_peak": st.get("drift_peak", 0),
                    "holds": st.get("holds", 0),
                    "unattended": st.get("unattended", False),
                    "observe": st.get("observe", False),
                    "suspended": st["suspended"],
                    "suspended_reason": st["suspended_reason"],
                    "task": st["tasks"][-1] if st["tasks"] else "",
                    "carry": st["carry"],
                }
            )
        return out

    def events(self, session_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)
            ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]
