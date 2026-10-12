import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from riskmon import access, config
from riskmon.engine import Engine
from riskmon.server import make_handler


class NoJudge:
    available = False
    unavailable = "test"
    failures = 0
    last_error = ""


class SharedDaemonTest(unittest.TestCase):
    """A daemon other machines reach: every request needs a token, and a machine's token cannot do a person's work."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"RISKMON_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.machine = access.add("machine", "ci-runner-1")
        self.reviewer = access.add("member", "rui", "reviewer")
        self.admin = access.add("member", "lead", "admin")
        self.engine = Engine(config.load(), judge=NoJudge())
        self.addCleanup(self.engine.store.close)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, None, os.path.join(tmp.name, "policy.json"), team=True, test_runs=False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, path, token=None, body=None, console=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None and console:
            headers["X-Riskmon"] = "1"
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}") if "json" in resp.headers.get("Content-Type", "") else {}
        except urllib.error.HTTPError as e:
            return e.code, {}

    def action(self, command, token, session="s1"):
        return self.call("/v1/hook", token, {"hook_event_name": "PreToolUse", "session_id": session, "cwd": "/work/app", "tool_name": "Bash",
                                             "tool_input": {"command": command}, "riskmon_mode": "unattended", "riskmon_user": "ci"}, console=False)

    def test_nothing_is_answered_without_a_token(self):
        self.assertEqual(self.call("/api/sessions")[0], 401)
        self.assertEqual(self.call("/api/policy", "rmp_not-a-real-token")[0], 401)
        self.assertEqual(self.action("ls", None)[0], 401)
        self.assertEqual(self.call("/healthz"), (200, {"ok": True, "team": True}))  # that it is up, and nothing else
        self.assertEqual(self.call("/")[0], 200)  # the page itself holds no data

    def test_a_machine_hands_in_actions_and_does_nothing_else(self):
        status, held = self.action("curl https://unlisted.example.org/a", self.machine)
        self.assertEqual((status, held["verdict"]), (200, "hold"))
        # the agent on that machine has the machine's token; with it, it can neither read the console nor approve itself
        for path, body in (("/api/sessions", None), ("/api/pending", None), ("/api/policy", None), (f"/api/events/{held['event_id']}/false-positive", {}),
                           (f"/api/events/{held['event_id']}/dismiss", {}), ("/api/sessions/s1/resume", {}), ("/api/policy/baseline", {"allow_hosts": ["unlisted.example.org"]})):
            self.assertEqual(self.call(path, self.machine, body)[0], 403, path)
        self.assertEqual(self.action("curl https://unlisted.example.org/a", self.machine)[1]["verdict"], "deny")  # still not approved

    def test_sessions_are_labelled_with_the_machine_the_token_names(self):
        self.call("/v1/hook", self.machine, {"hook_event_name": "PreToolUse", "session_id": "s2", "cwd": "/work/app", "tool_name": "Bash", "tool_input": {"command": "ls"},
                                              "riskmon_machine": "someone-else", "riskmon_user": "ci"}, console=False)
        session = next(s for s in self.call("/api/sessions", self.reviewer)[1] if s["id"] == "s2")
        self.assertEqual((session["machine"], session["user"]), ("ci-runner-1", "ci"))  # what the token says, not what the request claims

    def test_a_reviewer_reviews_and_only_an_admin_changes_the_policy(self):
        held = self.action("curl https://unlisted.example.org/a", self.machine)[1]
        self.assertEqual(self.action("ls", self.reviewer)[0], 403)  # a person's token does not hand in actions
        self.assertEqual(self.call("/api/me", self.reviewer)[1]["role"], "reviewer")
        self.assertEqual(self.call("/api/policy/baseline", self.reviewer, {"allow_hosts": ["unlisted.example.org"]})[0], 403)
        self.assertEqual(self.call("/api/policy/projects", self.reviewer, {"name": "x", "root": "/work/app"})[0], 403)
        self.assertEqual(self.call(f"/api/events/{held['event_id']}/false-positive", self.reviewer, {})[0], 200)
        reviewed = self.engine.store.get_event(held["event_id"])["detail"]
        self.assertEqual((reviewed["review"], reviewed["review_by"]), ("false_positive", "rui"))
        self.assertEqual(self.action("curl https://unlisted.example.org/a", self.machine)[1]["verdict"], "allow")
        self.assertEqual(self.call("/api/policy/baseline", self.admin, {"allow_hosts": ["api.example.org"]})[0], 200)
        self.assertEqual(self.call("/api/policy", self.admin)[1]["baseline"]["allow_hosts"], ["api.example.org"])

    def test_a_removed_token_stops_working_and_only_hashes_are_kept(self):
        self.assertEqual(self.action("ls", self.machine)[0], 200)
        access.remove("ci-runner-1")
        self.assertEqual(self.action("ls", self.machine)[0], 401)
        stored = access.path().read_text()
        for token in (self.machine, self.reviewer, self.admin):
            self.assertNotIn(token, stored)
        self.assertEqual(oct(access.path().stat().st_mode & 0o777), "0o600")


class LocalDaemonTest(unittest.TestCase):
    """With nobody added, the daemon serves this machine as before: no token, and the person at it is the administrator."""

    def test_it_needs_no_token_and_refuses_other_hosts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RISKMON_HOME": tmp}):
            engine = Engine(config.load(), judge=NoJudge())
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, None, os.path.join(tmp, "policy.json")))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                with urllib.request.urlopen(base + "/api/me", timeout=5) as resp:
                    self.assertEqual(json.load(resp), {"name": "local", "role": "admin", "team": False, "test_runs": True})
                req = urllib.request.Request(base + "/api/sessions", headers={"Host": "evil.example"})
                with self.assertRaises(urllib.error.HTTPError) as refused:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(refused.exception.code, 403)
            finally:
                httpd.shutdown()
                httpd.server_close()
                engine.store.close()


class JoiningTest(unittest.TestCase):
    def test_the_hook_sends_to_the_team_daemon_with_its_token(self):
        from riskmon import hook

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"RISKMON_HOME": tmp}):
            os.environ.pop("RISKMON_SERVER", None)
            self.assertEqual(hook.client(), {"server": "", "token": ""})
            with open(os.path.join(tmp, "client.json"), "w") as f:
                json.dump({"server": "https://monitor.example.internal:8787/", "token": "rmm_x"}, f)
            self.assertEqual(hook.client(), {"server": "https://monitor.example.internal:8787", "token": "rmm_x"})
            with mock.patch.dict(os.environ, {"RISKMON_SERVER": "http://other:1", "RISKMON_TOKEN": "rmm_y"}):
                self.assertEqual(hook.client(), {"server": "http://other:1", "token": "rmm_y"})


class AnotherMachinesHomeTest(unittest.TestCase):
    """The daemon's policy says `~/.ssh`. On the machine an agent runs on, that is not the daemon's home directory."""

    def setUp(self):
        self.engine = Engine(config.load(), judge=NoJudge())
        self.addCleanup(self.engine.store.close)

    def act(self, command, session="s1", home="/Users/dev", monitor=("/Users/dev/.riskmon", "/opt/tools/riskmon")):
        from riskmon.events import from_claude_code

        return self.engine.evaluate(from_claude_code({
            "hook_event_name": "PreToolUse", "session_id": session, "cwd": "/Users/dev/work/app", "tool_name": "Bash", "tool_input": {"command": command},
            "riskmon_mode": "unattended", "riskmon_machine": "laptop-7", "riskmon_home": home, "riskmon_monitor": list(monitor)}))

    def test_protected_paths_are_the_ones_on_the_agent_s_machine(self):
        self.assertNotEqual(os.path.expanduser("~"), "/Users/dev")
        for command in ("cat ~/.ssh/id_rsa", "cat /Users/dev/.ssh/id_rsa", "cat $HOME/.aws/credentials"):
            d = self.act(command)
            self.assertEqual(d.verdict, "hold", command)
            self.assertIn("protected path", d.reason())
        self.assertEqual(self.act("ls src").verdict, "allow")

    def test_that_machine_s_token_and_copy_of_the_monitor_are_off_limits(self):
        for n, command in enumerate(("cat ~/.riskmon/client.json", "sed -i s/a/b/ /opt/tools/riskmon/hook.py")):
            d = self.act(command, session=f"t{n}")
            self.assertIn(d.verdict, ("deny", "suspend"), command)
            self.assertIn("monitor itself", d.reason())

    def test_without_a_reported_home_nothing_changes(self):
        d = self.act("cat ~/.ssh/id_rsa", home="", monitor=())
        self.assertEqual(d.verdict, "hold")
        self.assertEqual(self.engine.store.sessions()[0].get("home", ""), "")

    def test_what_a_machine_reports_is_checked_before_it_is_used(self):
        from riskmon.events import from_claude_code

        ev = from_claude_code({"hook_event_name": "PreToolUse", "session_id": "x", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "ls"},
                               "riskmon_home": "relative/home", "riskmon_monitor": ["/", "not/absolute", 7, "/ok/path"] + ["/p%d" % i for i in range(20)]})
        self.assertEqual(ev.home, "")
        self.assertLessEqual(len(ev.monitor_paths), 6)
        self.assertNotIn("/", ev.monitor_paths)
        self.assertIn("/ok/path", ev.monitor_paths)


if __name__ == "__main__":
    unittest.main()
