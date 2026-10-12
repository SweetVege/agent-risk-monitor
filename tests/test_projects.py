import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from riskmon import access, config, insights, projects, repo
from riskmon.engine import Engine
from riskmon.events import from_claude_code
from riskmon.server import make_handler


class NoJudge:
    available = False
    unavailable = "test"
    failures = 0
    last_error = ""


def checkout(root: Path, url: str, remote: str = "origin") -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "{remote}"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/{remote}/*\n[branch "main"]\n\tremote = {remote}\n')
    return root


class RepositoryTest(unittest.TestCase):
    """Which repository a directory belongs to, read without starting git."""

    def test_one_repository_reads_the_same_however_it_was_cloned(self):
        for url in ("git@github.com:Acme/Billing.git", "https://github.com/acme/billing", "https://github.com/acme/billing.git/", "ssh://git@github.com:22/acme/billing.git",
                    "https://someone:s3cret@github.com/acme/billing.git"):
            self.assertEqual(repo.normalize(url), "github.com/acme/billing", url)
        self.assertEqual(repo.normalize("https://gitlab.example.com/group/sub/tool.git"), "gitlab.example.com/group/sub/tool")
        for not_a_remote in ("", "/srv/git/billing.git", "../billing", "https://github.com/", "https://github.com/acme/../other"):
            self.assertEqual(repo.normalize(not_a_remote), "", not_a_remote)

    def test_found_from_anywhere_inside_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = checkout(Path(tmp).resolve() / "billing", "git@github.com:acme/billing.git")
            (root / "src" / "deep").mkdir(parents=True)
            self.assertEqual(repo.find(str(root / "src" / "deep")), ("github.com/acme/billing", str(root)))
            self.assertEqual(repo.find(tmp), ("", ""))  # not in a repository
            lone = checkout(Path(tmp).resolve() / "fork", "https://github.com/me/fork.git", remote="upstream")
            self.assertEqual(repo.find(str(lone))[0], "github.com/me/fork")  # no origin, and only one remote to mean

    def test_a_linked_worktree_belongs_to_the_main_checkout_s_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = checkout(Path(tmp).resolve() / "billing", "https://github.com/acme/billing.git")
            linked = root / ".git" / "worktrees" / "hotfix"
            linked.mkdir(parents=True)
            (linked / "commondir").write_text("../..\n")
            tree = Path(tmp).resolve() / "hotfix"
            tree.mkdir()
            (tree / ".git").write_text(f"gitdir: {linked}\n")
            self.assertEqual(repo.find(str(tree)), ("github.com/acme/billing", str(tree)))


class ProjectMatchTest(unittest.TestCase):
    def setUp(self):
        self.billing = projects.clean_project({"name": "billing", "repo": "git@github.com:Acme/Billing.git", "read_only": ["vendor"]})
        self.local = projects.clean_project({"name": "scratch", "root": "/srv/scratch"})
        self.all = [self.billing, self.local]

    def test_a_project_is_a_repository_a_directory_or_both(self):
        self.assertEqual((self.billing["repo"], self.billing["root"]), ("github.com/acme/billing", ""))
        self.assertEqual(projects.clean_project({"name": "x", "repo": "github.com/acme/tool"})["repo"], "github.com/acme/tool")
        for bad in ({"name": "x"}, {"name": "x", "repo": "billing"}, {"name": "x", "repo": "github.com/acme"}, {"name": "x", "root": "relative/dir"}):
            with self.assertRaises(ValueError, msg=bad):
                projects.clean_project(bad)

    def test_the_repository_decides_wherever_the_checkout_is(self):
        for root in ("/home/ci/work/billing", "/Users/dev/src/billing-2"):
            project, where = projects.match(self.all, root + "/src", "github.com/acme/billing", root)
            self.assertEqual((project["name"], where), ("billing", root))
        self.assertEqual(projects.match(self.all, "/srv/scratch/a", "", ""), (self.local, "/srv/scratch"))
        self.assertEqual(projects.match(self.all, "/elsewhere", "github.com/acme/other", "/elsewhere"), (None, ""))

    def test_a_name_given_at_launch_comes_first_and_an_unknown_one_changes_nothing(self):
        self.assertEqual(projects.match(self.all, "/srv/scratch", "", "", "billing"), (self.billing, "/srv/scratch"))
        self.assertEqual(projects.match(self.all, "/srv/scratch", "", "", "no-such-project"), (self.local, "/srv/scratch"))

    def test_relative_paths_are_taken_from_the_checkout_on_that_machine(self):
        cfg = config.load()
        effective = projects.apply(cfg, self.billing, "/home/ci/work/billing")
        self.assertIn("/home/ci/work/billing/vendor", effective["scope"]["read_only"])


class SessionsAcrossMachinesTest(unittest.TestCase):
    """One daemon, the same repository on two machines at different paths, and a console that can tell them apart."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"RISKMON_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        billing = projects.clean_project({"name": "billing", "repo": "github.com/acme/billing", "read_only": ["vendor"]})
        self.engine = Engine(config.load(), judge=NoJudge(), project_policies=[billing])
        self.addCleanup(self.engine.store.close)
        self.member = access.add("member", "rui", "reviewer")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.engine, None, os.path.join(tmp.name, "policy.json"), team=True, test_runs=False))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def act(self, session, machine, root, command, repository="github.com/acme/billing"):
        return self.engine.evaluate(from_claude_code({
            "hook_event_name": "PreToolUse", "session_id": session, "cwd": root, "tool_name": "Bash", "tool_input": {"command": command}, "riskmon_mode": "unattended",
            "riskmon_machine": machine, "riskmon_user": "ci", "riskmon_repo": repository, "riskmon_root": root if repository else ""}))

    def get(self, path):
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_address[1]}{path}", headers={"Authorization": "Bearer " + self.member})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_the_project_s_limits_follow_the_repository_to_each_machine(self):
        self.assertEqual(self.act("a", "runner-1", "/home/ci/work/billing", "sed -i s/1/2/ vendor/lib.py").verdict, "deny")
        self.assertEqual(self.act("b", "runner-2", "/builds/7/billing", "sed -i s/1/2/ /builds/7/billing/vendor/lib.py").verdict, "deny")
        # the same command in a repository the project does not name meets no such limit
        self.assertEqual(self.act("c", "runner-2", "/builds/7/other", "sed -i s/1/2/ vendor/lib.py", repository="github.com/acme/other").verdict, "allow")
        by_id = {s["id"]: s for s in self.engine.store.sessions()}
        self.assertEqual([(by_id[k]["machine"], by_id[k]["project"], by_id[k]["repo"]) for k in "abc"],
                         [("runner-1", "billing", "github.com/acme/billing"), ("runner-2", "billing", "github.com/acme/billing"), ("runner-2", "", "github.com/acme/other")])

    def test_the_project_is_fixed_when_the_session_starts(self):
        self.act("a", "runner-1", "/builds/1/other", "ls", repository="github.com/acme/other")
        # a later action claiming the project's repository does not move the session into it, or out of it
        self.assertEqual(self.act("a", "runner-1", "/builds/1/other", "sed -i s/1/2/ vendor/lib.py").verdict, "allow")
        self.assertEqual(self.engine.store.sessions()[0]["project"], "")

    def test_the_console_narrows_to_a_machine_or_a_project(self):
        self.act("a", "runner-1", "/home/ci/work/billing", "curl https://unlisted.example.org/a")
        self.act("b", "runner-2", "/builds/7/billing", "curl https://unlisted.example.org/b")
        self.act("c", "runner-2", "/builds/7/other", "curl https://unlisted.example.org/c", repository="github.com/acme/other")
        whole = self.get("/api/overview")
        self.assertEqual((whole["machines"], whole["projects"], whole["pending"], whole["sessions_all"]), (["runner-1", "runner-2"], ["billing"], 3, 3))
        two = self.get("/api/overview?machine=runner-2")
        self.assertEqual((two["pending"], two["pending_here"], two["sessions_all"], two["actions_all"]), (3, 2, 2, 2))
        self.assertEqual([p["session_id"] for p in self.get("/api/pending?machine=runner-2&project=billing")], ["b"])
        self.assertEqual([p["session_id"] for p in self.get("/api/pending?project=-")], ["c"])  # sessions in no project
        self.assertEqual(sorted(s["id"] for s in self.get("/api/sessions?project=billing")), ["a", "b"])
        self.assertEqual([i["machine"] for i in self.get("/api/incidents?machine=runner-1")], ["runner-1"])
        self.assertEqual(len(insights.incidents(self.engine.store)), 3)

    def test_who_resumed_a_session_is_kept(self):
        for n in range(3):
            self.act("a", "runner-1", "/home/ci/work/billing", f"sed -i s/1/{n}/ vendor/lib{n}.py")
        self.assertTrue(self.engine.store.sessions()[0]["suspended"])
        self.engine.resume("a", "rui")
        session = self.engine.store.sessions()[0]
        self.assertEqual((session["suspended"], session["resumed_by"]), (False, ["rui"]))


if __name__ == "__main__":
    unittest.main()
