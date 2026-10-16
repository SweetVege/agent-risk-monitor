# agent-risk-monitor

**Lets autonomous coding agents work alone, and stops them before they cross a line.**

Every action an agent takes (a shell command, a file it reads or writes, a request it makes) is checked before it runs. Ordinary work goes straight through. An action that goes beyond the limits you set is refused, held until a person approves it, or ends the session. It works with Claude Code today.

## Why

An agent left alone with a task it cannot finish honestly will often find another way. In our test runs, five models were each given such tasks twenty times per condition:

- **Three of the five used another project's API token** to get a failing script to work, one of them in 19 of 20 runs, although a README next to the token said it must not be copied.
- **One model wrote the data itself** and reported the sync done.
- **One model edited code the repository said not to touch**, in 18 of 20 runs.

With the monitor refusing, the other project's token reached no agent in 100 runs. Every run can be replayed step by step on the [evaluation results page](https://sweetvege.github.io/agent-risk-monitor/).

## See it

**[Try the console in your browser.](https://sweetvege.github.io/agent-risk-monitor/demo/)** It is the real page with made-up sample data: review a held action, open a session, change a policy.

[![The console: one action waiting for review with its run paused, one suspended session, and what was stopped this week](docs/img/console-protect.jpg)](https://sweetvege.github.io/agent-risk-monitor/demo/)

## What it does

- **Checks every action before it runs.** Rules look at where the action reaches: hosts off an allowlist, protected paths such as `~/.ssh`, anything outside the workspace, paths you declared read-only, the monitor itself. A model judge can weigh the unclear cases against what the user asked for; it is not part of this repository.
- **Follows your policy.** A baseline for every session, and additions per project, matched by repository wherever it is checked out: the hosts a project may call, the paths that must not change there.
- **Is built for unattended runs.** When nobody is at the keyboard, an action that needs a person is held, not asked about. The agent is told which limit it met and carries on with something else. A machine can instead pause the run for a set time while a reviewer decides.
- **Shows you what happened.** A console with what is waiting for review, what was stopped, and every session step by step, where held actions are approved or kept refused.
- **Serves a team.** One daemon for many machines, each with its own token; reviewers sign in with theirs. Notices go to Slack or any webhook.

## Quick start

Python 3.10 or later, on macOS.

```bash
pip install git+https://github.com/SweetVege/agent-risk-monitor
riskmon service install   # keep the daemon running; it starts at every login
riskmon init --user       # protect every Claude Code session you start from now on
riskmon doctor            # check that everything is in place
```

Then open the console at http://127.0.0.1:8787. Each step is explained in [Using it](guide/usage.md).

## Learn more

| | |
|---|---|
| [Using it](guide/usage.md) | Installing, everyday tasks, the policy file, unattended runs |
| [Running it for a team](guide/team.md) | A shared daemon, tokens and roles, notices, pausing for a reviewer; [`deploy/`](deploy/README.md) hosts it in a container |
| [How it decides](guide/how-it-works.md) | How an action is scored, the scope policy, what goes back to the agent, the full list of limits |
| [Evaluation results](https://sweetvege.github.io/agent-risk-monitor/) | Five models on two tasks built to tempt them, with the monitor recording or refusing |

## Limits

This is early work. The ones that matter most:

- **It sees only the agent's own tool calls.** What a script or background process the agent starts goes on to do is invisible to it.
- **It is not an isolation boundary.** The monitor and the agent run on the same machine as the same user. A deployment that has to hold against a determined agent needs the monitor out of the agent's reach and egress control at the system level.
- **The rules read command text.** A path or host reached through variables or aliases can get past them.
- **It has been tried narrowly.** Claude Code is the only agent supported, macOS the only system tested, and the thresholds have not been calibrated on real use.

[How it decides](guide/how-it-works.md#known-limits) has the full list.

## This repository

This is the open core: the hook, the rules, the scope policy, the daemon and console, the activity report, unattended runs and the team features. Two parts used in the experiments are not included: the model judge and the drift score. The monitor runs without them; [When there is no judge](guide/how-it-works.md#when-there-is-no-judge) says what that changes.

Released under the [MIT licence](LICENSE).
