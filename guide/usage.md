# Using it

How to install the monitor, keep it running, and work with it day to day. For a team sharing one daemon, see [Running it for a team](team.md). For what the monitor checks and why, see [How it decides](how-it-works.md).

## Getting started

Python 3.10 or later, on macOS. The core uses only the standard library.

**1. Install**

```bash
pip install git+https://github.com/SweetVege/agent-risk-monitor
```

Or, from a checkout, run `pip install .`

**2. Keep the daemon running**

```bash
riskmon service install
```

On macOS this registers the daemon with launchd for the current user: it starts now, starts at every login, and is restarted if it stops. It needs no administrator rights and writes no credential into the service definition. `riskmon service show` prints what would be registered without changing anything, and `riskmon service remove` undoes it.


The daemon listens on this machine only, and the console is at http://127.0.0.1:8787. To run it by hand instead, in a terminal you leave open:

```bash
riskmon serve
```

**3. Turn protection on**

```bash
riskmon init --user
```

This writes four hooks into your Claude Code settings and leaves everything else there alone. Every Claude Code session you start **after this** is monitored, in any project, with nothing to set up per project. `riskmon init --user --remove` takes the hooks out.

To cover one project only, run `riskmon init` inside it; the hooks then go into that project's `.claude/settings.json`.

**4. Check**

```bash
riskmon doctor
```

It lists the state of the daemon, the judge if there is one, and the protection, and says what to do about anything missing.

When the daemon is not running, the hook denies actions such as network access and reads of sensitive files and lets the rest through. That is why step 2 comes before step 3: protection that covers every session should not depend on remembering to start something.

## Everyday use

| To | Do this |
|---|---|
| See what is waiting for you and what was stopped | The console's Protect page, at http://127.0.0.1:8787 |
| See what a session touched | Open it in the console, or `riskmon report <session id>` |
| Resume a suspended session | "Resume session" in the console, or `riskmon resume <session id>` |
| Approve a held action, or keep it refused | "Approve" or "Keep it refused" in the console's review queue |
| Overturn a wrong verdict | "Mark false positive" on that action in the session |
| Give a project its own scope | "Add a project" on the Policy page, by repository or by directory |
| Run a task unattended | `riskmon run --cwd <project> "task"`; needs the `claude` CLI |
| Be told when something needs you | `riskmon notify add <name> --slack`; see [Running it for a team](team.md#telling-people-and-waiting-for-them) |
| Change the policy | The console's Policy page; changes take effect on each session's next action. Thresholds and other keys: `~/.riskmon/policy.json` with just the keys you change, then restart the daemon |

A policy example that adds an API you use to the host allowlist and raises the deny line:

```json
{
  "allow_hosts": ["github.com", "pypi.org", "api.open-meteo.com"],
  "thresholds": { "deny": 80 }
}
```

Dictionaries are merged level by level and lists are replaced whole, so the `allow_hosts` above replaces the default allowlist. Every setting and its default is in `riskmon/default_policy.json`.

The policy and the audit data live in `~/.riskmon/`; set `RISKMON_HOME` to move them. Running straight from a checkout (`python3 -m riskmon ...`) uses the checkout itself.
