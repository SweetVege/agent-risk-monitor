"""The riskmon command line: serve, service, team, join, leave, init, doctor, run, report, resume, hook."""
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
    s.add_argument("--listen", default="127.0.0.1", help="address to listen on; anything but this machine's own makes the daemon a shared one, where every request needs a token")
    s.add_argument("--cert", help="TLS certificate chain, for a shared daemon")
    s.add_argument("--key", help="the certificate's private key")
    tm = sub.add_parser("team", help="on a shared daemon: who may hand in actions and who may use the console")
    tm.add_argument("action", choices=["add-machine", "add-member", "remove", "list"])
    tm.add_argument("name", nargs="?")
    tm.add_argument("--role", choices=["admin", "reviewer"], default="reviewer", help="for a member: a reviewer approves held actions; an admin also changes the policy")
    jn = sub.add_parser("join", help="send this machine's sessions to a team's daemon, and turn protection on")
    jn.add_argument("--server", required=True, help="the daemon's address, such as https://monitor.example.internal:8787")
    jn.add_argument("--token-file", help="read the machine token from this file; without it you are asked, and it is not shown as you type")
    sub.add_parser("leave", help="stop sending this machine's sessions to a team's daemon")
    r = sub.add_parser("resume", help="resume a suspended session")
    r.add_argument("session_id")
    i = sub.add_parser("init", help="turn protection on: for every session on this machine with --user, or for one project")
    i.add_argument("--user", action="store_true", help="cover every Claude Code session this user starts, in any project")
    i.add_argument("--project", default=".", help="project directory (default: the current one)")
    i.add_argument("--remove", action="store_true", help="take our hooks out again")
    sv = sub.add_parser("service", help="keep the daemon running: start it at login and restart it if it stops")
    sv.add_argument("action", choices=["install", "remove", "status", "show"], help="show prints what would be registered and changes nothing")
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
    u.add_argument("--project", default="", help="the project whose policy this run follows, by name; without it the repository or the directory decides")
    u.add_argument("--allow-dir", action="append", default=[], help="an extra directory this run may touch (repeatable)")
    args = ap.parse_args()
    if args.cmd == "hook":  # every action passes through here, so do nothing extra
        from .hook import cli

        return cli()
    if args.cmd == "doctor":
        from . import doctor

        sys.exit(0 if doctor.check(args.project) else 1)
    if args.cmd == "team":
        from . import access

        if args.action == "list":
            data = access.load()
            for kind, entries in (("machine", data["machines"]), ("member", data["members"])):
                for e in entries:
                    print(f"{kind:8} {e['name']:30} {e.get('role', '')}")
            return print("Nobody yet. The daemon serves this machine only until a machine or a member is added.") if not access.in_use() else None
        if not args.name:
            sys.exit("give a name")
        try:
            if args.action == "remove":
                return print("Removed; its token no longer works." if access.remove(args.name) else "No machine or member has that name.")
            kind = "machine" if args.action == "add-machine" else "member"
            token = access.add(kind, args.name, args.role if kind == "member" else "")
        except ValueError as e:
            sys.exit(str(e))
        print(f"{kind.capitalize()} {args.name!r} added" + (f" as {args.role}" if kind == "member" else "") + ". Its token, shown this once and not kept:\n")
        print("  " + token + "\n")
        print("On that machine:  riskmon join --server <this daemon's address>" if kind == "machine" else "They paste it into the console's sign-in page.")
        return print("Restart the daemon if it was serving this machine only: from now on every request needs a token.")
    if args.cmd in ("join", "leave"):
        import getpass
        import urllib.error

        from . import install

        target = config.home() / "client.json"
        if args.cmd == "leave":
            was = target.exists()
            target.unlink(missing_ok=True)
            return print("This machine no longer sends its sessions to a team's daemon. Protection stays on, against the daemon on this machine." if was else "This machine had not joined one.")
        server = args.server.rstrip("/")
        if not server.startswith(("http://", "https://")):
            sys.exit("give the address with its scheme, such as https://monitor.example.internal:8787")
        token = Path(args.token_file).read_text(encoding="utf-8").strip() if args.token_file else getpass.getpass("Machine token (not shown): ").strip()
        # ask the daemon who this token is before saving anything
        probe = urllib.request.Request(server + "/v1/hook", data=b'{"hook_event_name": "Ping"}', headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(probe, timeout=10) as resp:
                json.load(resp)
        except urllib.error.HTTPError as e:
            sys.exit(f"The daemon at {server} did not accept this token ({e.code}). A machine token is needed here, not a member's.")
        except OSError as e:
            sys.exit(f"Could not reach {server}: {e}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"server": server, "token": token}) + "\n", encoding="utf-8")
        target.chmod(0o600)
        path, _ = install.apply(user=True)
        print(f"Joined {server}. Every Claude Code session this user starts is now checked there.\n"
              f"The token is kept in {target}, readable by this user only; the hooks are in {path}.\n"
              "If the daemon cannot be reached, ordinary work goes through and anything beyond the default policy is refused.")
        return
    if args.cmd == "service":
        from . import service

        if args.action == "show":
            import plistlib

            return print(plistlib.dumps(service.definition(args.policy)).decode())
        if args.action == "status":
            state = service.running()
            return print("not set up on this system" if state is None else "registered with launchd; it starts at login" if state else "not registered: riskmon service install")
        try:
            if args.action == "install":
                print(f"Registered {service.install(args.policy)}.\nThe daemon is starting now and will start at every login. Its output goes to {config.data_dir() / 'daemon.log'}.")
            else:
                print("Removed. The daemon is stopped and will not start at login." if service.remove() else "It was not registered.")
        except RuntimeError as e:
            sys.exit(str(e))
        return
    if args.cmd == "init":
        from . import install

        path, touched = install.apply(args.project, args.remove, args.user)
        if not touched:
            print(f"{path} needs no change: " + ("our hooks are not in it." if args.remove else "the hooks are already installed."))
        else:
            print(f"{'Removed from' if args.remove else 'Wrote to'} {path}: {', '.join(touched)}")
            if not args.remove and args.user:
                print("Every Claude Code session this user starts from now on is monitored, in any project.\n"
                      "Keep the daemon running (riskmon serve): while it is not, the hook lets ordinary work through and\n"
                      "refuses what goes beyond the policy, such as network access and reads of protected paths.")
            elif not args.remove:
                print("Only Claude Code sessions started in this project from now on are monitored.")
        return
    cfg = config.load(args.policy)

    if args.cmd == "serve":
        data = Path(args.data)
        data.mkdir(parents=True, exist_ok=True)
        serve(Engine(cfg, Store(str(data / "riskmon.db"))), cfg["port"], args.policy, args.listen, args.cert, args.key)
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

        sys.exit(run(args.task, args.cwd, cfg["port"], args.max_turns, args.allow_host, args.allow_dir, args.observe, args.model, args.project))
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
