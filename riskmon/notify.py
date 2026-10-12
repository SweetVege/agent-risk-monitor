"""Tell people when something needs them: an action held for review, or a session suspended.

A notice is posted to each configured webhook, as plain JSON or in the form Slack's incoming webhooks
take. It says what kind of limit was met and where. The command itself and the task are left out unless
a webhook is set up to carry them: they go to another service, and a command can hold a credential.

A webhook's address is a secret (whoever has it can post there), so it is kept in a file only this
user can read, set from the command line and never from the console's page. Sending happens on its own
thread: a slow or failing webhook never delays a verdict.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import config

EVENTS = ("held", "suspended")
FORMATS = ("json", "slack")
TIMEOUT = 6
_NAME = re.compile(r"^[\w][\w.-]{0,39}$")
HEADLINE = {"held": "An action is waiting for review", "suspended": "A session was suspended", "test": "Test notice from the risk monitor"}


def path() -> Path:
    return config.home() / "notify.json"


def load() -> dict:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    hooks = [h for h in raw.get("webhooks", []) if isinstance(h, dict) and h.get("name") and h.get("url")] if isinstance(raw, dict) else []
    return {"console_url": str(raw.get("console_url", "")) if isinstance(raw, dict) else "", "webhooks": hooks}


def save(cfg: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(target, 0o600)


def clean_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("a webhook is an http or https address")
    return url


def add(name: str, url: str, fmt: str = "json", on: tuple = EVENTS, with_command: bool = False) -> None:
    if not _NAME.match(name or ""):
        raise ValueError("a webhook needs a short name of letters, digits, dots or dashes")
    if fmt not in FORMATS or not on or any(e not in EVENTS for e in on):
        raise ValueError("unknown format or event")
    cfg = load()
    cfg["webhooks"] = [h for h in cfg["webhooks"] if h["name"] != name] + [{"name": name, "url": clean_url(url), "format": fmt, "on": list(on), "with_command": bool(with_command)}]
    save(cfg)


def remove(name: str) -> bool:
    cfg = load()
    kept = [h for h in cfg["webhooks"] if h["name"] != name]
    if len(kept) == len(cfg["webhooks"]):
        return False
    save({**cfg, "webhooks": kept})
    return True


def set_console_url(url: str) -> None:
    save({**load(), "console_url": clean_url(url).rstrip("/") if url else ""})


def shown(hook: dict) -> str:
    """A webhook for a listing: where it goes, without the part of the address that is the secret."""
    return f"{hook['name']}: {hook.get('format', 'json')} to {urlparse(hook['url']).hostname}, on {' and '.join(hook.get('on', EVENTS))}" + (", with the command" if hook.get("with_command") else "")


def _where(n: dict) -> str:
    place = " as ".join(x for x in (n.get("machine", ""), n.get("user", "")) if x)
    return " · ".join(x for x in (place, "project " + n["project"] if n.get("project") else "") if x)


def _link(n: dict, console_url: str) -> str:
    if not console_url or not n.get("session_id"):
        return ""
    return f"{console_url}/#/protect/session/{urllib.request.quote(str(n['session_id']), safe='')}" + (f"?e={n['event_id']}" if n.get("event_id") else "")


def body(hook: dict, notice: dict, console_url: str = "") -> dict:
    """What is posted to one webhook for one notice."""
    full = bool(hook.get("with_command"))
    link = _link(notice, console_url)
    wait = int(notice.get("wait") or 0)
    waiting = f"The run is paused for up to {max(1, round(wait / 60))} min for a decision." if wait else ""
    if hook.get("format") == "slack":
        esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines = [f"*{esc(HEADLINE[notice['event']])}*" + (f": {esc(notice['kind'])}" if notice.get("kind") else ""), esc(_where(notice)), esc(waiting)]
        if full and notice.get("target"):
            lines.append("```" + esc(notice["target"][:500]).replace("```", "'''") + "```")
        if full and notice.get("reason"):
            lines.append(esc(notice["reason"][:400]))
        lines.append(f"<{link}|Open it in the console>" if link else "")
        return {"text": "\n".join(x for x in lines if x)}
    out = {k: notice.get(k, "") for k in ("event", "kind", "event_id", "session_id", "machine", "user", "project", "ts")}
    out.update({"summary": HEADLINE[notice["event"]], "wait_seconds": wait, "url": link})
    if full:
        out.update({"target": str(notice.get("target", ""))[:2000], "reason": str(notice.get("reason", ""))[:1000], "task": str(notice.get("task", ""))[:500]})
    return out


def post(hook: dict, notice: dict, console_url: str = "") -> None:
    data = json.dumps(body(hook, notice, console_url), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(hook["url"], data=data, headers={"Content-Type": "application/json", "User-Agent": "riskmon"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        resp.read(200)


class Notifier:
    """Posts notices in the background. The configuration is read afresh for each one, so a webhook added while the daemon runs is used at once."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self.sent, self.failed, self.last_error = 0, 0, ""
        threading.Thread(target=self._run, daemon=True).start()

    def __call__(self, notice: dict) -> None:
        try:
            self._queue.put_nowait({**notice, "ts": notice.get("ts") or time.time()})
        except queue.Full:
            self.failed += 1  # a flood of notices is dropped, not allowed to pile up or slow anything down

    def _run(self) -> None:
        while True:
            notice = self._queue.get()
            try:
                cfg = load()
                for hook in cfg["webhooks"]:
                    if notice["event"] not in hook.get("on", EVENTS):
                        continue
                    try:
                        post(hook, notice, cfg["console_url"])
                        self.sent += 1
                    except Exception as e:  # one webhook failing must not stop the others, or the thread
                        self.failed += 1
                        self.last_error = f"{hook['name']}: {type(e).__name__}"  # not the message: it can repeat the address
            finally:
                self._queue.task_done()

    def drain(self, timeout: float = 5.0) -> None:
        """Wait until what was queued has been tried. For tests and for the `notify test` command."""
        end = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < end:
            time.sleep(0.02)
