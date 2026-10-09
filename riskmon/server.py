"""Local daemon: the hook posts actions here to be scored and the console reads from it. Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import report
from .engine import Engine
from .events import from_claude_code
from .optional import drift

_DASHBOARD = Path(__file__).with_name("dashboard.html")
_MAX_BODY = 4 * 1024 * 1024


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # the audit log is in SQLite; no access log on stderr
            pass

        def _send(self, code: int, body, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _local(self) -> bool:
            # refuse DNS rebinding: only accept requests addressed to this machine
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > _MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            if url.path == "/healthz":
                judge = engine.judge
                return self._send(200, {"ok": True, "judge": judge.available, "judge_note": judge.unavailable or judge.last_error})
            if url.path == "/api/sessions":
                sessions = engine.store.sessions()
                for s in sessions:
                    s["drift_level"] = drift.level(s["drift_peak"], engine.cfg)
                return self._send(200, sessions)
            if url.path == "/api/events":
                sid = parse_qs(url.query).get("session_id", [""])[0]
                return self._send(200, engine.store.events(sid))
            if url.path == "/api/report":
                sid = parse_qs(url.query).get("session_id", [""])[0]
                return self._send(200, report.build(engine.store, sid, engine.cfg))
            if url.path == "/":
                return self._send(200, _DASHBOARD.read_bytes(), "text/html")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            try:
                payload = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})

            if path == "/v1/hook":
                name = payload.get("hook_event_name")
                if name == "PreToolUse":
                    d = engine.evaluate(from_claude_code(payload))
                    return self._send(
                        200, {"verdict": d.verdict, "event_id": d.event_id, "score": d.score, "reason": d.reason()}
                    )
                if name == "UserPromptSubmit":
                    prompt = payload.get("prompt") or payload.get("prompt_text") or ""
                    st = engine.record_task(str(payload.get("session_id", "unknown")), str(prompt))
                    return self._send(200, {"suspended": st.suspended, "reason": st.suspended_reason})
                if name in ("PostToolUse", "PostToolUseFailure"):
                    ok = name == "PostToolUse"
                    out = payload.get("tool_response") if ok else payload.get("error")
                    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    found = engine.record_outcome(
                        str(payload.get("session_id", "unknown")),
                        str(payload.get("tool_use_id", "")),
                        ok,
                        text,
                        str(payload.get("transcript_path", "")),
                    )
                    return self._send(200, {"recorded": found})
                return self._send(200, {"verdict": "allow"})

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[3] in ("resume", "false-positive"):
                # a custom header forces a preflight on cross-site browser requests, and we don't answer preflights
                if self.headers.get("X-Riskmon") != "1":
                    return self._send(403, {"error": "missing X-Riskmon header"})
                if parts[1] == "sessions" and parts[3] == "resume":
                    engine.resume(parts[2])
                    return self._send(200, {"ok": True})
                if parts[1] == "events" and parts[3] == "false-positive" and parts[2].isdigit():
                    try:
                        engine.mark_false_positive(int(parts[2]))
                    except KeyError:
                        return self._send(404, {"error": "no such event"})
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def serve(engine: Engine, port: int) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine))
    judge = engine.judge
    print(f"riskmon listening on http://127.0.0.1:{port}")
    print(f"L2 judge: {'on (' + judge.cfg['model'] + ')' if judge.available else 'off — ' + judge.unavailable}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
