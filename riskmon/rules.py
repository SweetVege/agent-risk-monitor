"""L0: a capability allowlist.

It answers one question, whether an action goes beyond what the policy allows, and does not try
to recognise techniques. What is allowed comes entirely from the policy (defaults in
default_policy.json): the host allowlist, protected paths and command classes.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .events import ActionEvent

_URL = re.compile(r"https?://[^\s'\"<>)]+", re.I)
_USER_AT_HOST = re.compile(r"(?<![\w.])[\w.-]+@([\w-]+(?:\.[\w-]+)+)")
_PUNCT = "();<>|&\n"
_ENV_ASSIGN = re.compile(r"^\w+=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.S)
_PLAUSIBLE_NAME = re.compile(r"^[\w./+-]+$")
_WRAPPERS = {"env", "time", "nohup", "xargs", "command", "exec"}
# the shell's own syntax and builtins, not external programs
_KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "esac", "in", "{", "}", "!", "[[", "]]"}
_DECLARATIONS = {"for", "case", "select", "function"}  # a declaration header: no program to run follows in this segment
_BUILTINS = {
    "shopt", "set", "unset", "local", "declare", "return", "read", "exit", "trap", "wait", "break",
    "continue", "type", "hash", "alias", "printf", "echo", "test", "[", ":", "cd", "pwd", "export",
}
_INLINE_FLAGS = {"-c", "-e"}
_TEMPLATE_ENV = (".example", ".sample", ".template")


@dataclass
class L0Result:
    scores: dict = field(default_factory=lambda: {"boundary": 0, "stealth": 0, "resource": 0})
    facets: set = field(default_factory=set)
    findings: list = field(default_factory=list)
    hosts: list = field(default_factory=list)
    staged: bool = False  # the written file refers to both a protected path and an unlisted host
    net_hosts: list = field(default_factory=list)  # unlisted hosts that appear in the written file
    outside: list = field(default_factory=list)  # locations outside the workspace this action touches
    searches: list = field(default_factory=list)  # outside searches, as written for the report
    unknown: list = field(default_factory=list)  # programs outside the known lists

    def hit(self, dim: str, score: int, facet: str, note: str) -> None:
        self.scores[dim] = max(self.scores[dim], score)
        self.facets.add(facet)
        if note not in self.findings:
            self.findings.append(note)

    @property
    def top(self) -> int:
        return max(self.scores.values())


def _host_allowed(host: str, cfg: dict) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in cfg["allow_hosts"])


def _is_internal(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (ip.is_private or ip.is_link_local) and not ip.is_loopback


def _hosts_in(text: str, logins: bool = False) -> list[str]:
    """With logins=True, user@host counts as a host too. Only for network commands, or an email in a commit message would count."""
    hosts = [urlparse(u).hostname or "" for u in _URL.findall(text)]
    if logins:
        hosts += _USER_AT_HOST.findall(text)
    return sorted({h.lower() for h in hosts if h})


def _expand(text: str) -> str:
    home = os.path.expanduser("~")
    text = text.replace("${HOME}", home).replace("$HOME", home)
    return re.sub(r"(^|[\s'\"=:])~(?=/|\s|$)", lambda m: m.group(1) + home, text)


def _is_env_file(path: str) -> bool:
    name = os.path.basename(path)
    return name == ".env" or (name.startswith(".env.") and not name.endswith(_TEMPLATE_ENV))


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots if r)


def _touches_monitor(text: str, cfg: dict) -> bool:
    """The monitor's own directories or port. The hook config is judged separately, since reading it is common and harmless."""
    port = cfg["port"]
    return any(m in text for m in cfg["monitor_paths"]) or re.search(rf"(localhost|127\.0\.0\.1|\[::1\]):{port}\b", text) is not None


_HOOK_CONFIG = ".claude/settings"
_READERS = {"cat", "head", "tail", "less", "more", "grep", "rg", "wc", "ls", "stat", "file", "diff", "jq", "echo", "find"}
_GIT_READS = {"diff", "log", "show", "status", "blame", "ls-files", "check-ignore"}
_REDIRECT_INTO_HOOK_CONFIG = re.compile(r">\s*[\"']?[^\s;|&]*\.claude/settings")


_HOOK_FILES = ("settings.json", "settings.local.json")
_LISTERS = {"ls", "tree", "stat", "du", "cd"}  # given a directory, these show names and sizes, not contents
_REDIRECT_OUT = re.compile(r"(?<![0-9&])>>?(?!&)\s*(?!/dev/null)")


def _glob_match(path: str, pattern: str) -> bool:
    """Whether a shell would expand `pattern` to `path`. Wildcards do not cross a slash or match a leading dot."""
    have, want = path.split("/"), pattern.split("/")
    return len(have) == len(want) and all(
        fnmatch.fnmatchcase(h, w) and not (h.startswith(".") and w[:1] in "*?[") for h, w in zip(have, want)
    )


def _hook_config_users(progs: list, cwd: str) -> tuple[list, bool]:
    """The programs in a command that are handed the hook config, and whether the command works from inside its directory.

    The file's name need not appear: `cat .claude/*` and `cd .claude && cat settings.json` reach it
    too. Arguments are resolved against the directory the command has cd'ed to and matched as a
    shell would expand them, against the project's and the user's hook config.
    """
    dirs = [os.path.join(base, ".claude") for base in (cwd or "/", os.path.expanduser("~"))]
    files = [os.path.join(d, f) for d in dirs for f in _HOOK_FILES]
    users, inside, here = [], False, cwd or "/"

    def resolve(arg: str) -> str:
        raw = _expand(arg)
        return os.path.normpath(raw if os.path.isabs(raw) else os.path.join(here, raw))

    for name, args, _ in progs:
        if name == "cd":
            here = resolve(args[0]) if args and not args[0].startswith("-") else os.path.expanduser("~")
            inside = inside or here in dirs
            continue
        for arg in args:
            if arg.startswith("-") and "=" in arg:
                arg = arg.split("=", 1)[1]
            if arg.startswith("-"):
                continue
            path = resolve(arg)
            whole_dir = path in dirs and not (name in _LISTERS or (name == "find" and not {"-exec", "-execdir", "-delete", "-ok"} & set(args)))
            if _HOOK_CONFIG in _expand(arg) or whole_dir or any(_glob_match(f, path) for f in files):
                users.append((name, args))
                break
    return users, inside


def _only_reads_hook_config(command: str, users: list, inside: bool) -> bool:
    """Whether every program in the command that touches the hook config only reads it.

    Only programs that take the file as an argument count: in `cat .claude/settings.json; env | grep X`
    the env has nothing to do with it and must not turn the whole command into tampering.
    """
    if _REDIRECT_INTO_HOOK_CONFIG.search(command):
        return False
    if inside and _REDIRECT_OUT.search(_HEREDOC.sub("", command)):
        return False  # writes somewhere after cd'ing into the hook config's directory; the target's name alone will not say where
    if not users:  # the name only appears inside a quoted script or inline code; no telling what is done to it
        return False
    return all(name in _READERS or (name == "git" and args[0] in _GIT_READS) for name, args in users)


_SEARCHERS = {"find", "grep", "egrep", "fgrep", "rg", "fd", "ag", "du", "tree"}
_WHOLE_DISK = {"mdfind", "locate", "plocate"}
_PATH_ARG = re.compile(r"^(~|/|\.\./|\.\.$)")


def _region(path: str) -> str:
    """Map a path to a coarse location, to count how many places outside the workspace a session has touched."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        rest = path[len(home) :].strip("/").split("/")
        if len(rest) == 1 and rest[0].startswith("."):
            return "~ (dotfiles)"  # ~/.zshrc, ~/.profile and the like are one place, or checking five files makes five locations
        return "~/" + rest[0] if rest[0] else "~"
    parts = path.strip("/").split("/")
    return "/" + parts[0]


def _outside_paths(progs: list, cwd: str, cfg: dict) -> list[tuple[str, bool]]:
    """Paths outside the workspace that a command touches, and whether it searches them.

    Follows cd within the command: `cd ~/x && find .` searches ~/x, not the workspace.
    """
    skip = [cwd, *cfg["scope"]["extra_dirs"], *cfg["monitor_paths"], "/dev", *cfg["temp_dirs"], *cfg["protected_paths"]]

    def resolve(arg: str, base: str) -> str:
        raw = _expand(arg)
        return os.path.normpath(raw if os.path.isabs(raw) else os.path.join(base, raw))

    out, here = [], cwd
    for name, args, _ in progs:
        if name == "cd":
            here = resolve(args[0], here) if args and not args[0].startswith("-") else os.path.expanduser("~")
            if not _under(here, skip):
                out.append((here, False))
            continue
        flags = "".join(a for a in args if a.startswith("-") and not a.startswith("--"))
        if name in _WHOLE_DISK:
            out.append(("/", True))  # Spotlight and locate index the whole machine wherever they are run from
            continue
        searching = name in _SEARCHERS or (name == "ls" and "R" in flags)
        if searching and not _under(here, skip):  # already cd'ed outside, so a relative search is a search outside
            out.append((here, True))
        for arg in args:
            if arg.startswith("-") and "=" in arg:
                arg = arg.split("=", 1)[1]
            if "://" in arg or not _PATH_ARG.match(arg):
                continue
            path = resolve(arg, here)
            if not _under(path, skip):  # protected paths and the monitor's directories have heavier rules of their own
                out.append((path, searching))
    return out


def _is_operator(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def _programs(command: str) -> tuple[list[tuple[str, list[str], bool]], bool]:
    """Split a shell command into [(program, args, reads from a pipe)], plus whether it ends in &.

    Tokenised with shlex, so newlines, semicolons and & inside quotes are not separators. Heredoc
    bodies are dropped first: they are data, not commands. Unbalanced quotes fall back to splitting
    on lines and whitespace.
    """
    # find's \( \) is its own grouping syntax; left in, shlex unescapes them and the parentheses read as separators
    body = _HEREDOC.sub("", command).replace("\\(", " ").replace("\\)", " ")
    try:
        lex = shlex.shlex(body, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace, lex.whitespace_split = " \t\r", True
        toks = list(lex)
    except ValueError:
        toks = [t for line in body.splitlines() for t in line.split() + ["\n"]]

    segments, cur, piped_in = [], [], False
    skip = False
    for tok in toks:
        if skip:  # the target of a redirection
            skip = False
        elif _is_operator(tok):
            if "<" in tok or ">" in tok:
                if cur and cur[-1].isdigit():  # the file descriptor in 2>&1
                    cur.pop()
                skip = True
                continue
            segments.append((cur, piped_in))
            cur, piped_in = [], tok.startswith("|") and not tok.startswith("||")
        else:
            cur.append(tok)
    segments.append((cur, piped_in))
    detached = bool(toks) and toks[-1] == "&" or body.rstrip().endswith("&")

    out = []
    for seg, piped in segments:
        if seg and seg[0] in _DECLARATIONS:
            continue
        if seg[:2] in (["command", "-v"], ["command", "-V"]):
            continue  # only asks whether a program exists; does not run it
        while seg and (_ENV_ASSIGN.match(seg[0]) or seg[0] in _KEYWORDS or os.path.basename(seg[0]) in _WRAPPERS):
            seg = seg[1:]
            while seg and seg[0].startswith("-"):  # the wrapper's own options, as in xargs -n1 or env -i
                seg = seg[1:]
        if seg:
            out.append((os.path.basename(seg[0]) if "$" not in seg[0] else seg[0], seg[1:], piped))
    return out, detached


def _check_hosts(res: L0Result, hosts: list[str], cfg: dict, unlisted_score: int, what: str) -> None:
    res.hosts = hosts
    for h in hosts:
        if _is_internal(h):
            res.hit("boundary", 60, "internal_address", f"{what} reaches the private or link-local address {h}")
        elif not _host_allowed(h, cfg):
            res.hit("boundary", unlisted_score, "egress", f"{what} targets {h}, which is not on the allowlist")


def _assess_shell(ev: ActionEvent, cfg: dict, res: L0Result) -> None:
    cmds = cfg["commands"]
    text = _expand(ev.target)
    progs, detached = _programs(ev.target)
    names = [p for p, _, _ in progs]
    uses_network = any(n in cmds["network"] for n in names)
    hosts = _hosts_in(ev.target, logins=uses_network)
    users, inside = _hook_config_users(progs, ev.cwd)

    if _touches_monitor(text, cfg):
        res.hit("stealth", 95, "monitor_tamper", "command touches the monitor itself (its directory or port)")
    elif _HOOK_CONFIG in text or users or (inside and _REDIRECT_OUT.search(_HEREDOC.sub("", ev.target))):
        if _only_reads_hook_config(ev.target, users, inside):
            res.hit("stealth", 60, "monitor_probe", "reads the hook config")
        else:
            res.hit("stealth", 95, "monitor_tamper", "command may modify the hook config")
    if any(n in cmds["privilege"] for n in names):
        res.hit("boundary", 55, "privilege", "command needs elevated privileges")

    if uses_network:
        if hosts:
            _check_hosts(res, hosts, cfg, 50, "network command")
            if not res.facets & {"egress", "internal_address"}:
                res.hit("boundary", 5, "network", "network command to an allowlisted host")
        else:
            res.hit("boundary", 35, "egress", "network command with no identifiable target host")
    elif hosts:
        _check_hosts(res, hosts, cfg, 30, "URL in command")

    for p in cfg["protected_paths"]:
        if p in text:
            res.hit("resource", 55, "protected_access", f"command touches the protected path {p}")
    if any(_is_env_file(t) for _, args, _ in progs for t in args):
        res.hit("resource", 35, "protected_access", "command touches a .env file")

    # widening scope: the command reaches outside the workspace. File tools always had their paths checked; paths in commands did not
    if ev.cwd:
        home = os.path.expanduser("~")
        for path, searching in _outside_paths(progs, ev.cwd, cfg):
            res.outside.append(_region(path))
            if not searching:
                res.hit("boundary", 10, "outside_workspace", f"command touches {path}, outside the workspace")
            elif path in ("/", home, os.path.dirname(home)):
                res.hit("boundary", 30, "outside_search", f"searches all of {path}")
                res.searches.append(f"searches all of {path}")
                res.facets.add("whole_search")
            else:
                res.hit("boundary", 20, "outside_search", f"searches {path}, outside the workspace")
                res.searches.append(f"searches {path}, outside the workspace")

    heredoc = _HEREDOC.search(ev.target) is not None
    for name, args, piped in progs:
        if name in cmds["interpreter"] or name == "eval":
            if name == "eval" or _INLINE_FLAGS & set(args) or heredoc:
                res.hit("stealth", 25, "inline_code", "inline code, opaque to the rule layer")
            elif piped and all(a.startswith("-") for a in args):
                score = 80 if uses_network else 45
                res.hit("stealth", score, "piped_exec", "pipes upstream output straight into an interpreter")
        elif name in cmds["package"]:
            res.hit("boundary", 10, "package", "installs or runs a third-party package")
        elif name in cmds["probe"]:
            res.hit("boundary", 15, "env_probe", f"inspects system or network configuration ({name})")
        elif "$" in name:
            res.hit("boundary", 15, "unknown_program", "runs a program through a variable, so the rule layer cannot tell what runs")
        elif not _PLAUSIBLE_NAME.match(name) or name in _BUILTINS or name in _WHOLE_DISK:
            continue  # case patterns, leftover syntax fragments, shell builtins
        elif not any(name in cmds[k] for k in ("safe", "network", "privilege")):
            res.hit("boundary", 15, "unknown_program", f"program {name} is not on the known list")
            res.unknown.append(name)

    if detached or "nohup" in ev.target.split():
        res.hit("stealth", 15, "detached", "starts a background process the hook cannot follow")


def _assess_file(ev: ActionEvent, cfg: dict, res: L0Result) -> None:
    write = ev.kind == "file_write"
    raw = _expand(ev.target)
    if not raw:
        return
    path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(ev.cwd or "/", raw))
    in_workspace = bool(ev.cwd) and _under(path, [ev.cwd, *cfg["scope"]["extra_dirs"]])

    own = _under(path, cfg["monitor_paths"])
    if own or _HOOK_CONFIG in path:
        if write:
            res.hit("stealth", 95, "monitor_tamper", "modifies the monitor itself or the hook config")
        elif own:
            # the monitored agent must not see the policy, the rules or the judge prompt; this one skips the judge
            res.hit("stealth", 75, "monitor_probe", "reads the monitor's own policy or code")
        else:
            res.hit("stealth", 60, "monitor_probe", "reads the hook config")
    if _under(path, cfg["protected_paths"]):
        res.hit("resource", 65 if write else 55, "protected_access", f"touches the protected path {path}")
    elif _is_env_file(path):
        res.hit("resource", 35, "protected_access", "touches a .env file")

    if write and _under(path, cfg["system_dirs"]):
        res.hit("boundary", 65, "system_write", f"writes to the system directory {path}")
        res.facets.add("outside_write")
    elif not in_workspace and not _under(path, cfg["temp_dirs"]):
        res.hit("boundary", 30 if write else 10, "outside_workspace", "touches a path outside the workspace")
        if write:
            res.facets.add("outside_write")
        res.outside.append(_region(path))

    if write and ev.content:
        body = _expand(ev.content)
        unlisted = [h for h in _hosts_in(body) if not _host_allowed(h, cfg)]
        protected = [p for p in cfg["protected_paths"] if p in body]
        res.net_hosts = unlisted
        if unlisted and protected:
            res.staged = True
            res.hit("resource", 40, "staged", "the written file refers to both a protected path and an unlisted host")


def _assess_network(ev: ActionEvent, cfg: dict, res: L0Result) -> None:
    if ev.target.startswith("search://"):
        res.hit("boundary", 5, "network", "web search")
        return
    if _touches_monitor(ev.target, cfg):
        res.hit("stealth", 95, "monitor_tamper", "request targets the monitor's own port")
    _check_hosts(res, _hosts_in(ev.target), cfg, 20, "web fetch")
    if len(urlparse(ev.target).query) > 300:
        res.hit("boundary", 45, "large_query", "unusually long URL query string, which may carry data")


def assess(ev: ActionEvent, cfg: dict) -> L0Result:
    res = L0Result()
    if ev.kind == "shell":
        _assess_shell(ev, cfg, res)
    elif ev.kind in ("file_read", "file_write"):
        _assess_file(ev, cfg, res)
    elif ev.kind == "network":
        _assess_network(ev, cfg, res)
    elif ev.kind == "other":
        text = _expand(ev.target)
        res.hit("boundary", 10, "external_tool", "calls an external tool (MCP and the like)")
        if any(p in text for p in cfg["protected_paths"]):
            res.hit("resource", 55, "protected_access", "a protected path appears in the external tool's arguments")
    return res
