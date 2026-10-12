import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from riskmon import access, config, hook, install, notify
from riskmon.engine import Engine
from riskmon.events import from_claude_code
from riskmon.server import make_handler


class NoJudge:
    available = False
    unavailable = "test"
    failures = 0
    last_error = ""


NOTICE = {"event": "held", "kind": "A host off the allowlist", "event_id": 7, "session_id": "s 1", "machine": "ci-runner-1", "user": "ci", "project": "billing",
          "wait": 0, "target": "Bash: curl -H 'Authorization: Bearer abc123' https://x.example.org/<a>", "reason": "network command targets x.example.org", "task": "sync the rates", "ts": 1.0}


class Home(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        env = mock.patch.dict(os.environ, {"RISKMON_HOME": tmp.name, "CLAUDE_CONFIG_DIR": os.path.join(tmp.name, "claude")})
        env.start()
        self.addCleanup(env.stop)

    def receiver(self, status=200):
        """A webhook on this machine that keeps what is posted to it."""
        got = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(status)
                self.end_headers()

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}/hooks/T000/secret-part", got


class NoticeTest(Home):
    """What is posted, and what is kept back."""

    def test_the_command_and_the_task_stay_home_unless_asked_for(self):
        plain = notify.body({"format": "json"}, NOTICE, "https://monitor.example.internal:8787")
        self.assertEqual((plain["event"], plain["kind"], plain["machine"], plain["project"]), ("held", "A host off the allowlist", "ci-runner-1", "billing"))
        self.assertEqual(plain["url"], "https://monitor.example.internal:8787/#/protect/session/s%201?e=7")
        self.assertNotIn("abc123", json.dumps(plain))
        self.assertNotIn("sync the rates", json.dumps(plain))
        full = notify.body({"format": "json", "with_command": True}, NOTICE)
        self.assertIn("abc123", full["target"])
        self.assertEqual(full["url"], "")  # no console address set, so no link rather than a wrong one

    def test_slack_gets_text_with_its_markup_escaped(self):
        text = notify.body({"format": "slack", "with_command": True}, {**NOTICE, "wait": 600}, "https://m.example")["text"]
        self.assertIn("*An action is waiting for review*: A host off the allowlist", text)
        self.assertIn("ci-runner-1 as ci · project billing", text)
        self.assertIn("paused for up to 10 min", text)
        self.assertIn("&lt;a&gt;", text)
        self.assertIn("<https://m.example/#/protect/session/s%201?e=7|Open it in the console>", text)
        self.assertNotIn("abc123", notify.body({"format": "slack"}, NOTICE)["text"])

    def test_a_webhook_s_address_is_kept_private(self):
        notify.add("ops", "https://hooks.slack.com/services/T000/B000/secretpart", "slack")
        self.assertEqual(stat.S_IMODE(os.stat(notify.path()).st_mode), 0o600)
        self.assertNotIn("secretpart", notify.shown(notify.load()["webhooks"][0]))
        self.assertIn("hooks.slack.com", notify.shown(notify.load()["webhooks"][0]))
        for bad in ("ftp://example.org/x", "hooks.slack.com/services/x", ""):
            with self.assertRaises(ValueError):
                notify.add("bad", bad)
        self.assertTrue(notify.remove("ops"))
        self.assertEqual(notify.load()["webhooks"], [])


class EngineNoticeTest(Home):
    def setUp(self):
        super().setUp()
        self.engine = Engine(config.load(), judge=NoJudge())
        self.addCleanup(self.engine.store.close)
        self.engine.notify = self.notifier = notify.Notifier()

    def act(self, command, session="s1", wait=None):
        payload = {"hook_event_name": "PreToolUse", "session_id": session, "cwd": "/work/app", "tool_name": "Bash", "tool_input": {"command": command}, "riskmon_mode": "unattended"}
        if wait is not None:
            payload["riskmon_wait"] = wait
        return self.engine.evaluate(from_claude_code(payload))

    def test_a_held_action_and_a_suspension_are_each_told_once(self):
        url, got = self.receiver()
        notify.add("all", url)
        notify.add("suspensions", url, "slack", ("suspended",))
        self.assertEqual(self.act("ls").verdict, "allow")
        self.assertEqual(self.act("curl https://unlisted.example.org/a").verdict, "hold")
        self.notifier.drain()
        self.assertEqual([(g["event"], g["kind"], g["event_id"]) for g in got], [("held", "A host off the allowlist", 2)])
        for n in range(3):
            self.act(f"curl https://unlisted.example.org/a")  # retrying a held action is refused, and three refusals suspend
        self.assertEqual(self.act("ls").verdict, "suspend")  # already suspended: nothing new to tell
        self.notifier.drain()
        self.assertEqual([g.get("event") or g["text"].splitlines()[0] for g in got[1:]], ["suspended", "*A session was suspended*: A host off the allowlist"])

    def test_a_webhook_that_fails_changes_nothing_but_the_count(self):
        url, _ = self.receiver(status=500)
        notify.add("broken", url)
        notify.add("gone", "http://127.0.0.1:9/nothing-listens-here")
        self.assertEqual(self.act("curl https://unlisted.example.org/a").verdict, "hold")
        self.notifier.drain()
        self.assertEqual((self.notifier.sent, self.notifier.failed), (0, 2))
        self.assertNotIn("secret-part", self.notifier.last_error)
        self.engine.notify = lambda notice: 1 / 0
        self.assertEqual(self.act("curl https://unlisted.example.org/b").verdict, "hold")


class WaitForReviewTest(Home):
    """A machine can be set up to pause a run while a person decides a held action. It is off unless asked for."""

    def setUp(self):
        super().setUp()
        self.machine, self.other = access.add("machine", "ci-runner-1"), access.add("machine", "ci-runner-2")
        self.engine = Engine(config.load(), judge=NoJudge())
        self.addCleanup(self.engine.store.close)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, None, os.path.join(self.home, "policy.json"), team=True, test_runs=False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.server = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def run_hook(self, command, wait=0, token=None, decide=None):
        """Run the hook as Claude Code would, optionally with a person deciding the first held action a moment later."""
        payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/work/app", "tool_name": "Bash", "tool_input": {"command": command}}
        env = {"RISKMON_SERVER": self.server, "RISKMON_TOKEN": token or self.machine, "RISKMON_MODE": "unattended"}
        if decide:
            def later():
                deadline = time.time() + 5
                while time.time() < deadline:
                    held = [e for e in self.engine.store.events("s1") if e["verdict"] == "hold" and not e["detail"].get("review")]
                    if held:
                        return getattr(self.engine, decide)(held[0]["id"], by="rui")
                    time.sleep(0.05)
            threading.Thread(target=later, daemon=True).start()
        out, started = io.StringIO(), time.time()
        argv = ["hook"] + (["--wait", str(wait)] if wait else [])
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.argv", argv), redirect_stdout(out):
            hook.main()
        text = out.getvalue()
        return (json.loads(text)["hookSpecificOutput"]["permissionDecisionReason"] if text.strip() else "allowed"), time.time() - started

    def test_without_it_a_held_action_comes_straight_back(self):
        said, took = self.run_hook("curl https://unlisted.example.org/a")
        self.assertIn("Held for human review", said)
        self.assertLess(took, 1.5)
        self.assertEqual(self.engine.store.events("s1")[0]["detail"]["wait_until"], 0)

    def test_approved_while_waiting_the_action_runs(self):
        said, took = self.run_hook("curl https://unlisted.example.org/a", wait=20, decide="mark_false_positive")
        self.assertEqual(said, "allowed")
        self.assertLess(took, 5)
        self.assertGreater(self.engine.store.events("s1")[0]["detail"]["wait_until"], time.time())

    def test_kept_refused_while_waiting_the_agent_is_told_a_person_decided(self):
        said, _ = self.run_hook("curl https://unlisted.example.org/a", wait=20, decide="dismiss")
        self.assertIn("A reviewer looked at this action and kept it refused", said)

    def test_when_nobody_decides_in_time_it_is_held_as_before(self):
        said, took = self.run_hook("curl https://unlisted.example.org/a", wait=2)
        self.assertIn("Held for human review", said)
        self.assertTrue(1.5 < took < 6, took)

    def test_a_machine_cannot_watch_another_machine_s_reviews(self):
        self.run_hook("curl https://unlisted.example.org/a")
        event = self.engine.store.events("s1")[0]["id"]
        with mock.patch.dict(os.environ, {"RISKMON_SERVER": self.server, "RISKMON_TOKEN": self.other}):
            self.assertEqual(hook.wait_for_review({"session_id": "s1"}, event, 2, 0), "")
            with self.assertRaises(Exception):
                hook.ask_daemon({"hook_event_name": "ReviewWait", "session_id": "s1", "event_id": event, "riskmon_left": 1}, 0)

    def test_the_installed_hook_is_given_longer_than_it_waits(self):
        path, _ = install.apply(user=True, wait=600)
        hooks = json.loads(path.read_text())["hooks"]
        pre, post = hooks["PreToolUse"][0]["hooks"][0], hooks["PostToolUse"][0]["hooks"][0]
        self.assertTrue(pre["command"].endswith(" --wait 600"))
        self.assertGreater(pre["timeout"], 600 + 2 * hook.HTTP_TIMEOUT - 1)  # the first answer, the wait, and a last poll that times out
        self.assertEqual((post["timeout"], "--wait" in post["command"]), (20, False))
        self.assertEqual(install.installed_wait(user=True), 600)
        self.assertEqual(hook.own_wait(pre["command"].split()), 600)
        self.assertEqual(install.apply(user=True, wait=600)[1], [])
        self.assertEqual(install.apply(user=True)[1], ["PreToolUse"])  # and plain init takes the wait out again
        self.assertEqual(install.installed_wait(user=True), 0)


if __name__ == "__main__":
    unittest.main()
