"""Local daemon: the hook posts actions here to be scored and the console reads from it. Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, insights, projects, report
from .engine import Engine
from .events import from_claude_code
from .optional import drift

_PAGE = Path(__file__).with_name("app.html")
_MAX_BODY = 4 * 1024 * 1024


def policy_view(cfg: dict) -> dict:
    """The policy in force, as the console shows it: the limits a person set, without the rule tables behind them."""
    scope = cfg["scope"]
    return {
        "thresholds": cfg["thresholds"], "suspend_after_denies": cfg["suspend_after_denies"], "max_holds": cfg["unattended"]["max_holds"],
        "allow_hosts": cfg["allow_hosts"], "protected_paths": cfg["protected_paths"], "read_only": scope.get("read_only", []), "extra_dirs": scope.get("extra_dirs", []),
        "attended": scope.get("attended", {}), "unattended": scope.get("unattended", {}),
    }


def make_handler(engine: Engine, extension=None, policy_file=None):
    policy_file = policy_file or config.user_policy_path()
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
            if url.path == "/api/overview":
                return self._send(200, {**insights.overview(engine.store), "judge": engine.judge.available, "judge_note": engine.judge.unavailable or engine.judge.last_error,
                                        "test_area": extension is not None})
            if url.path == "/api/pending":
                return self._send(200, insights.pending(engine.store))
            if url.path == "/api/incidents":
                return self._send(200, insights.incidents(engine.store))
            if url.path == "/api/policy":
                return self._send(200, {"baseline": policy_view(engine.cfg), "projects": engine.projects, "rules": list(projects.RULES), "file": str(policy_file)})
            if url.path == "/":
                return self._send(200, _PAGE.read_bytes(), "text/html")
            if extension is not None and url.path.startswith("/test/"):
                found = extension.get(url.path[len("/test"):])
                if found is not None:
                    body, ctype, name = found
                    if name:
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                        self.end_headers()
                        return self.wfile.write(body)
                    return self._send(200, body, ctype.split(";")[0])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            try:
                payload = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})

            if extension is not None and path.startswith("/test/"):
                origin = self.headers.get("Origin") or ""
                if self.headers.get("X-Riskmon") != "1" or (origin and not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin)):
                    return self._send(403, {"error": "forbidden"})
                try:
                    return self._send(*extension.post(path[len("/test"):], payload))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})

            if path == "/v1/hook":
                name = payload.get("hook_event_name")
                if name == "PreToolUse":
                    d = engine.evaluate(from_claude_code(payload))
                    return self._send(
                        200, {"verdict": d.verdict, "event_id": d.event_id, "score": d.score, "reason": d.reason(), "agent_note": d.agent_note}
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

            if path.startswith("/api/policy/"):
                # the policy decides what every agent may do, so a change must come from the console's own page
                origin = self.headers.get("Origin") or ""
                if self.headers.get("X-Riskmon") != "1" or (origin and not re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin)):
                    return self._send(403, {"error": "forbidden"})
                try:
                    current = list(engine.projects)
                    if path == "/api/policy/baseline":
                        projects.save_baseline(policy_file, payload)
                    elif path == "/api/policy/projects":
                        project = projects.clean_project(payload)
                        was = payload.get("was") or project["name"]
                        current = [p for p in current if p["name"] not in (was, project["name"])] + [project]
                        projects.save(sorted(current, key=lambda p: p["name"].lower()))
                    elif path == "/api/policy/projects/delete":
                        current = [p for p in current if p["name"] != payload.get("name")]
                        projects.save(current)
                    else:
                        return self._send(404, {"error": "not found"})
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                engine.set_policy(config.load(policy_file if Path(policy_file).exists() else None), projects.load())
                return self._send(200, {"ok": True})

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[3] in ("resume", "false-positive", "dismiss"):
                # a custom header forces a preflight on cross-site browser requests, and we don't answer preflights
                if self.headers.get("X-Riskmon") != "1":
                    return self._send(403, {"error": "missing X-Riskmon header"})
                if parts[1] == "sessions" and parts[3] == "resume":
                    engine.resume(parts[2])
                    return self._send(200, {"ok": True})
                if parts[1] == "events" and parts[3] in ("false-positive", "dismiss") and parts[2].isdigit():
                    try:
                        (engine.mark_false_positive if parts[3] == "false-positive" else engine.dismiss)(int(parts[2]))
                    except KeyError:
                        return self._send(404, {"error": "no such event"})
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def serve(engine: Engine, port: int, policy_file=None) -> None:
    from .optional import test_area

    engine.projects = projects.load()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, test_area(), policy_file))
    except OSError as e:
        # usually a daemon that is already running, by hand or as the login service
        raise SystemExit(f"cannot listen on 127.0.0.1:{port}: {e.strerror}. Is the daemon already running? Check with: riskmon doctor")
    judge = engine.judge
    judge.check()
    print(f"riskmon listening on http://127.0.0.1:{port}")
    print(f"L2 judge: {'on (' + judge.cfg['model'] + ')' if judge.available else 'off — ' + judge.unavailable}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
