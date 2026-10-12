"""Local daemon: the hook posts actions here to be scored and the console reads from it. Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import access, config, insights, notify, projects, report
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


REVIEW_POLL = 8  # seconds one ReviewWait request is kept open; below the hook's own HTTP timeout


def make_handler(engine: Engine, extension=None, policy_file=None, team: bool = False, test_runs: bool = True):
    """`team`: the daemon is shared, so every request must carry a token and is held to what that token may do.
    Otherwise it serves this machine only and whoever is at it is the administrator."""
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

        def _same_site(self) -> bool:
            """A change must come from this console's own page. On a daemon for this machine that means a local origin;
            on a shared one the token already says who is asking, and a browser never attaches it for another site."""
            origin = self.headers.get("Origin") or ""
            return team or not origin or re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d+", origin) is not None

        def _local(self) -> bool:
            # refuse DNS rebinding: only accept requests addressed to this machine
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _caller(self) -> access.Caller | None:
            if not team:
                return access.LOCAL if self._local() else None
            auth = self.headers.get("Authorization") or ""
            return access.identify(auth[7:].strip()) if auth.startswith("Bearer ") else None

        def _refuse(self, caller) -> None:
            if caller is None:
                self._send(401 if team else 403, {"error": "sign in with a member token" if team else "forbidden"})
            else:
                self._send(403, {"error": "this token is not allowed to do that"})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > _MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            caller = self._caller()
            # the page and its script hold no data, and a browser cannot attach a token when it loads them
            if url.path == "/" and (team or self._local()):
                return self._send(200, _PAGE.read_bytes(), "text/html")
            if team and extension is not None and url.path in ("/test/app.js", "/test/app.css"):
                body, ctype, _ = extension.get(url.path[len("/test"):])
                return self._send(200, body, ctype)
            if url.path == "/healthz":
                judge = engine.judge
                if team and caller is None:
                    return self._send(200, {"ok": True, "team": True})  # reachable, and nothing more to someone unidentified
                return self._send(200, {"ok": True, "team": team, "judge": judge.available, "judge_note": judge.unavailable or judge.last_error})
            if caller is None or caller.kind != "member":
                return self._refuse(caller)
            if url.path == "/api/me":
                return self._send(200, {"name": caller.name, "role": caller.role, "team": team, "test_runs": test_runs})
            query = parse_qs(url.query)
            narrow = {"machine": query.get("machine", [""])[0][:80], "project": query.get("project", [""])[0][:80]}
            if url.path == "/api/sessions":
                sessions = [s for s in engine.store.sessions() if all(not v or s[k] == ("" if v == "-" else v) for k, v in narrow.items())]
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
                return self._send(200, {**insights.overview(engine.store, **narrow), "judge": engine.judge.available, "judge_note": engine.judge.unavailable or engine.judge.last_error,
                                        "test_area": extension is not None,
                                        "notices": {"webhooks": len(notify.load()["webhooks"]), "failed": getattr(engine.notify, "failed", 0), "last_error": getattr(engine.notify, "last_error", "")}})
            if url.path == "/api/pending":
                return self._send(200, insights.pending(engine.store, **narrow))
            if url.path == "/api/incidents":
                return self._send(200, insights.incidents(engine.store, **narrow))
            if url.path == "/api/policy":
                return self._send(200, {"baseline": policy_view(engine.cfg), "projects": engine.projects, "rules": list(projects.RULES), "file": str(policy_file)})
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
            caller = self._caller()
            if caller is None:
                return self._refuse(None)
            path = urlparse(self.path).path
            try:
                payload = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})

            if path == "/v1/hook":
                # On a shared daemon actions come from machines, each under its own token. A member's token is for the
                # console; taking actions from it would let a person's session pass as a machine's.
                if team and caller.kind != "machine":
                    return self._refuse(caller)
                if team:
                    payload["riskmon_machine"] = caller.name
            elif caller.kind != "member":
                return self._refuse(caller)  # a machine's token hands in actions and does nothing else

            if extension is not None and path.startswith("/test/"):
                if self.headers.get("X-Riskmon") != "1" or not self._same_site():
                    return self._send(403, {"error": "forbidden"})
                if not (caller.can_administer and test_runs):
                    return self._send(403, {"error": "evaluations are not started from this daemon" if not test_runs else "only an administrator starts evaluations"})
                try:
                    return self._send(*extension.post(path[len("/test"):], payload))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})

            if path == "/v1/hook":
                name = payload.get("hook_event_name")
                if name == "PreToolUse":
                    d = engine.evaluate(from_claude_code(payload))
                    return self._send(
                        200, {"verdict": d.verdict, "event_id": d.event_id, "score": d.score, "reason": d.reason(), "agent_note": d.agent_note, "wait": d.wait}
                    )
                if name == "ReviewWait":
                    # the hook of a paused run asks whether a person has decided its held action; answer when one has, or shortly
                    row = engine.store.get_event(payload["event_id"]) if isinstance(payload.get("event_id"), int) else None
                    mine = row is not None and row["session_id"] == str(payload.get("session_id")) and (
                        not team or engine.store.load_state(row["session_id"]).machine == caller.name)  # a machine learns about its own sessions only
                    if not mine:
                        return self._send(404, {"error": "no such held action"})
                    left = payload.get("riskmon_left")
                    end = time.time() + max(0.0, min(float(left) if isinstance(left, (int, float)) else 0.0, REVIEW_POLL))
                    while not (review := engine.store.get_event(row["id"])["detail"].get("review", "")) and time.time() < end:
                        time.sleep(0.25)
                    return self._send(200, {"review": review})
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
                if self.headers.get("X-Riskmon") != "1" or not self._same_site():
                    return self._send(403, {"error": "forbidden"})
                if not caller.can_administer:
                    return self._refuse(caller)
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
                    engine.resume(parts[2], caller.name)
                    return self._send(200, {"ok": True})
                if parts[1] == "events" and parts[3] in ("false-positive", "dismiss") and parts[2].isdigit():
                    try:
                        (engine.mark_false_positive if parts[3] == "false-positive" else engine.dismiss)(int(parts[2]), by=caller.name)
                    except KeyError:
                        return self._send(404, {"error": "no such event"})
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    return Handler


def serve(engine: Engine, port: int, policy_file=None, listen: str = "127.0.0.1", cert: str | None = None, key: str | None = None) -> None:
    from .optional import test_area

    engine.projects = projects.load()
    engine.notify = notify.Notifier()
    shared = listen not in ("127.0.0.1", "localhost", "::1")
    team = shared or access.in_use()
    if shared and not any(m.get("role") == "admin" for m in access.load()["members"]):
        raise SystemExit("A daemon other machines can reach needs an administrator first:\n  riskmon team add-member <name> --role admin")
    try:
        # on a shared daemon evaluations are not started from the console: that takes Docker and model keys on the server
        httpd = ThreadingHTTPServer((listen, port), make_handler(engine, test_area(), policy_file, team, test_runs=not team))
    except OSError as e:
        # usually a daemon that is already running, by hand or as the login service
        raise SystemExit(f"cannot listen on {listen}:{port}: {e.strerror}. Is the daemon already running? Check with: riskmon doctor")
    if cert:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    elif shared:
        print("Warning: listening beyond this machine without TLS. Tokens cross the network in the clear; give --cert and --key, or put this behind a proxy that terminates TLS.")
    judge = engine.judge
    judge.check()
    print(f"riskmon listening on {'https' if cert else 'http'}://{listen}:{port}" + (" (shared: every request needs a token)" if team else ""))
    print(f"L2 judge: {'on (' + judge.cfg['model'] + ')' if judge.available else 'off — ' + judge.unavailable}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
