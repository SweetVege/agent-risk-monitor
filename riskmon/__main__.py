"""The riskmon command line: serve, init, doctor, run, report, resume, hook."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from . import config
from .engine import Engine
from .server import serve
from .store import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="riskmon")
    ap.add_argument("--policy", help="policy overrides file holding only the keys you change (default: policy.json in the monitor's home directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the local daemon and console")
    s.add_argument("--data", default=str(config.data_dir()), help="audit data directory")
    r = sub.add_parser("resume", help="resume a suspended session")
    r.add_argument("session_id")
    i = sub.add_parser("init", help="install the hooks into a project's .claude/settings.json")
    i.add_argument("--project", default=".", help="project directory (default: the current one)")
    i.add_argument("--remove", action="store_true", help="take our hooks out again")
    d = sub.add_parser("doctor", help="check which setup step is still missing")
    d.add_argument("--project", default=".")
    sub.add_parser("hook", help="the hook entry point Claude Code calls; reads the event from stdin")
    sub.add_parser("hook-config", help="print the hooks configuration without writing it")
    rp = sub.add_parser("report", help="print a session's activity report: the files, hosts and programs it touched")
    rp.add_argument("session_id")
    rp.add_argument("--data", default=str(config.data_dir()))
    rp.add_argument("--cwd", default="", help="workspace; only needed for old sessions recorded without one")
    u = sub.add_parser("run", help="run one task unattended, with the monitor as the only gate")
    u.add_argument("task")
    u.add_argument("--cwd", default=".", help="project directory, with the hooks already installed")
    u.add_argument("--max-turns", type=int)
    u.add_argument("--model", help="model for this run, passed to the claude CLI (default: whatever the CLI is set to)")
    u.add_argument("--observe", action="store_true", help="for research: let would-be holds through and record them, to see what the agent does when nothing stops it")
    u.add_argument("--allow-host", action="append", default=[], help="an extra host this run may contact (repeatable)")
    u.add_argument("--allow-dir", action="append", default=[], help="an extra directory this run may touch (repeatable)")
    args = ap.parse_args()
    if args.cmd == "hook":  # every action passes through here, so do nothing extra
        from .hook import cli

        return cli()
    if args.cmd == "doctor":
        from . import doctor

        sys.exit(0 if doctor.check(args.project) else 1)
    if args.cmd == "init":
        from . import install

        path, touched = install.apply(args.project, args.remove)
        if not touched:
            print(f"{path} needs no change: " + ("our hooks are not in it." if args.remove else "the hooks are already installed."))
        else:
            print(f"{'Removed from' if args.remove else 'Wrote to'} {path}: {', '.join(touched)}")
            if not args.remove:
                print("Only Claude Code sessions started in this project from now on are monitored.")
        return
    cfg = config.load(args.policy)

    if args.cmd == "serve":
        data = Path(args.data)
        data.mkdir(parents=True, exist_ok=True)
        serve(Engine(cfg, Store(str(data / "riskmon.db"))), cfg["port"])
    elif args.cmd == "resume":
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg['port']}/api/sessions/{args.session_id}/resume",
            data=b"{}",
            headers={"X-Riskmon": "1", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(resp.read().decode())
    elif args.cmd == "run":
        from .runner import run

        sys.exit(run(args.task, args.cwd, cfg["port"], args.max_turns, args.allow_host, args.allow_dir, args.observe, args.model))
    elif args.cmd == "report":
        from . import report

        store = Store(str(Path(args.data) / "riskmon.db"))
        print(report.render(report.build(store, args.session_id, cfg, args.cwd)))
        store.close()
    elif args.cmd == "hook-config":
        from . import install

        print(json.dumps({"hooks": install.hooks_block()}, indent=2))

if __name__ == "__main__":
    main()
