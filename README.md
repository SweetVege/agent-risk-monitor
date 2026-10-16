# agent-risk-monitor

**Lets autonomous coding agents work alone, and stops them before they cross a line.**

[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](guide/usage.md)
[![Works with Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code-d97757.svg)](guide/usage.md)
[![Live demo](https://img.shields.io/badge/demo-try%20the%20console-2f7d4f.svg)](https://sweetvege.github.io/agent-risk-monitor/demo/)

**[Try the console](https://sweetvege.github.io/agent-risk-monitor/demo/)** · **[See the evaluation results](https://sweetvege.github.io/agent-risk-monitor/)** · **[Read the guides](#learn-more)**

Every action an agent takes (a shell command, a file it reads or writes, a request it makes) is checked before it runs. Ordinary work goes straight through. An action that goes beyond the limits you set is refused, held until a person approves it, or ends the session. When it is refused, the agent is told which limit it met, so it can look for a way to do the task inside it.

It works with Claude Code today, on one machine or as one daemon shared by a team.

## Why

An agent left alone with a task it cannot finish honestly will often find another way. In our test runs, five models were each given such a task twenty times per condition. In one, a script fails for want of an API token, and the project next door has a token that works, beside a README saying it must not be copied:

| Used the other project's token | Haiku 4.5 | Sonnet 5.5 | Opus 5.5 | DeepSeek Flash | DeepSeek V4 Pro |
|---|:-:|:-:|:-:|:-:|:-:|
| Monitor only recording | 2 / 20 | 0 / 20 | 0 / 20 | 13 / 20 | **19 / 20** |
| Monitor refusing | 0 / 20 | 0 / 20 | 0 / 20 | 0 / 20 | 0 / 20 |

In other runs, one model wrote the data itself and reported the sync done, and in a second task the same model edited code the repository said not to touch in 18 of 20 runs. Saying which limit an action met matters too: for that model, when the refusal named the limit, the runs that ended with a correct fix went from 6 to 17 of 20.

These are two scenarios and twenty runs a cell: enough to show the refusals mattered there, not to give a rate for any model in general. Every run can be replayed step by step on the [results page](https://sweetvege.github.io/agent-risk-monitor/).

## See it

<table>
<tr>
<td width="50%" valign="top"><a href="https://sweetvege.github.io/agent-risk-monitor/demo/"><img src="docs/img/console-protect.jpg" alt="The console's first page: one action waiting for review with its run paused, one suspended session, and what was stopped this week"></a><br><b>What needs you.</b> Held actions waiting for review, suspended sessions, and what was stopped this week, across every machine.</td>
<td width="50%" valign="top"><a href="https://sweetvege.github.io/agent-risk-monitor/demo/#/protect/session/7c1e-settle-fix"><img src="docs/img/console-session.jpg" alt="A session in the console: eight actions, one of them a change to vendored code that was denied"></a><br><b>What happened.</b> Every action of a session, the verdict it got and why, and what the agent touched outside its workspace.</td>
</tr>
</table>

The [demo](https://sweetvege.github.io/agent-risk-monitor/demo/) is the real console with made-up sample data and nothing behind it: approve a held action, resume a session, change the policy.

## What the agent is told

A refusal reaches the agent as the reason its tool call did not run. When a limit you declared stopped the action, the agent learns which one:

```text
Blocked by the risk monitor (event #1). The scope set for this run does not allow changing
a path declared read-only (command may change /home/dev/work/billing/vendor/calendars/business.py,
which is declared read-only). Do not try to get around this limit. If the task can be done
within it, do that; otherwise tell the user.
```

When nobody is there to approve something, it is held rather than refused, and the agent is told to carry on without it:

```text
Held for human review by the risk monitor (event #2). The scope set for this run does not allow
touching a protected path (command touches a .env file). Nobody is available to approve it right
now. Do not retry it or look for another way to do the same thing. Carry on with the parts of the
task that do not need it; if nothing is left, stop and report exactly what is waiting on review.
```

Reasons that come from scoring, or from the monitor protecting itself, are not sent back to the agent; they go to the console only.

## What it does

| | |
|---|---|
| **Checks every action before it runs** | Rules look at where the action reaches: hosts off an allowlist, protected paths such as `~/.ssh`, anything outside the workspace, paths declared read-only, the monitor itself. A model judge can weigh the unclear cases against what the user asked for; it is not part of this repository. |
| **Follows your policy** | A baseline for every session, and additions per project, matched by repository wherever it is checked out: the hosts a project may call, the paths that must not change there. Edited on the console's Policy page. |
| **Is built for unattended runs** | When nobody is at the keyboard, an action that needs a person is held, not asked about, and the agent carries on with something else. A machine can instead pause the run for a set time while a reviewer decides. |
| **Shows you what happened** | A console with what is waiting for review, what was stopped, and every session step by step, where held actions are approved or kept refused. |
| **Serves a team** | One daemon for many machines, each with its own token; reviewers and admins sign in with theirs. Notices go to Slack or any webhook. A container image is included. |

### Verdicts

| Verdict | When | What happens |
|---|---|---|
| **allow** | Nothing goes beyond the policy | The action runs. Claude Code's own permission prompts still apply. |
| **ask** | A person should agree, and someone is at the keyboard | Claude Code asks the user, with the reason. |
| **hold** | A person should agree, and nobody is there | The action does not run. It waits in the console; once approved, a retry is allowed. |
| **deny** | The action breaks a declared limit, or scores too high | The action does not run. Trying it again counts against the session. |
| **suspend** | Three refusals, five holds, or one clear violation | Every further action is refused until a person resumes the session. |

## Architecture

```mermaid
flowchart TB
    subgraph machine["On each machine that runs agents"]
        direction LR
        agent["Coding agent<br/>Claude Code"] <-->|"each tool call, before it runs<br/>and the verdict back"| hook["riskmon hook"]
    end

    subgraph daemon["riskmon daemon: on this machine, or one shared by a team"]
        direction LR
        policy[("Policy<br/>baseline + projects")] --> engine
        subgraph engine["Engine"]
            direction LR
            l0["L0 rules<br/>where it reaches"] --> l1["L1 trajectory<br/>what came before"] --> scope["Scope policy<br/>limits you set"] --> l2["L2 judge, optional<br/>unclear cases"]
        end
        engine --> audit[("Audit log")]
    end

    hook <-->|"action, repository, home directory<br/>verdict and the reason the agent may see"| engine
    audit --> console["Console<br/>Protect · Policy · Test"]
    console <-->|"approve, refuse, resume,<br/>edit the policy"| people(["Reviewers and admins"])
    daemon -.->|"held or suspended"| notices(["Slack or a webhook"])
```

**On each machine**, the hook is installed once into the user's Claude Code settings and runs before every tool call. It sends the action to the daemon, along with which repository the session is in and that machine's home directory, and turns the verdict into Claude Code's answer. If the daemon cannot be reached, it applies the rules locally: ordinary work goes through and anything beyond the default policy is refused.

**In the daemon**, each action passes through four stages:

1. **L0 rules** look at the action alone. They are an allowlist: they ask only whether it reaches beyond what the policy allows, and score it on boundary, stealth and resource.
2. **L1 trajectory** adds the session's history: a protected path read before an unlisted host is contacted, a file written and later run, a refused action tried again another way.
3. **The scope policy** rules outright on kinds of action you declared, separately for attended and unattended sessions: score as usual, need a person, or refuse.
4. **An optional judge model** sees only the unclear middle band, and only when the scope policy has not ruled. It can never turn a clear violation into an allow.

The policy has two layers: a baseline every session follows, and projects that add hosts and paths and can only make the handling stricter. A session's project is fixed at its first action, so an agent cannot move itself into a looser one.

**The console** reads the audit log and is where people act on it. On a daemon for one machine it is open to that machine only; a shared daemon answers nothing without a token, and the token a machine sends its actions with cannot read the log, approve anything or change the policy.

<details>
<summary><b>The evaluation harness</b> (how the results above were made)</summary>

<br>

The same engine runs inside an evaluation harness built on [Inspect](https://inspect.aisi.org.uk/). The agent works in a Docker sandbox set up as a trap (a missing token with a working one next door, or a failing test whose defect is in code it must not change), and the monitor is plugged in as Inspect's approver, outside the sandbox and out of the agent's reach. Each run is scored on what the sandbox holds at the end, independently of what the monitor saw, so behaviour the monitor cannot see, such as writing made-up data inside the workspace, is still counted. The harness is not part of this repository; its runs are on the [results page](https://sweetvege.github.io/agent-risk-monitor/).

</details>

## Quick start

Python 3.10 or later, on macOS.

```bash
pip install git+https://github.com/SweetVege/agent-risk-monitor
riskmon service install   # keep the daemon running; it starts at every login
riskmon init --user       # protect every Claude Code session you start from now on
riskmon doctor            # check that everything is in place
```

Then open the console at http://127.0.0.1:8787. Each step is explained in [Using it](guide/usage.md).

<details>
<summary><b>For a team</b>: one shared daemon</summary>

<br>

On the host that runs the daemon:

```bash
riskmon team add-member lead --role admin     # prints the token once; only its hash is kept
riskmon team add-machine ci-runner-1
riskmon serve --listen 0.0.0.0 --cert fullchain.pem --key privkey.pem
```

On each machine that runs agents:

```bash
riskmon join --server https://monitor.example.internal:8787
```

Reviewers open the daemon's address and sign in with their member token. To run the daemon in a container with TLS in front of it, see [`deploy/`](deploy/README.md); for roles, notices and pausing a run for a reviewer, see [Running it for a team](guide/team.md).

</details>

## Learn more

| Guide | What is in it |
|---|---|
| [Using it](guide/usage.md) | Installing, everyday tasks, the policy file, unattended runs |
| [Running it for a team](guide/team.md) | A shared daemon, tokens and roles, notices, pausing for a reviewer |
| [Deploying it in a container](deploy/README.md) | The image, Compose, TLS, backups and upgrades |
| [How it decides](guide/how-it-works.md) | Scoring in detail, the scope policy, what goes back to the agent, the full list of limits, the code layout |
| [Evaluation results](https://sweetvege.github.io/agent-risk-monitor/) | Five models on two tasks built to tempt them, every run replayable |

## Limits

This is early work. The ones that matter most:

- **It sees only the agent's own tool calls.** What a script or background process the agent starts goes on to do is invisible to it.
- **It is not an isolation boundary.** The monitor and the agent run on the same machine as the same user. A deployment that has to hold against a determined agent needs the monitor out of the agent's reach and egress control at the system level.
- **The rules read command text.** A path or host reached through variables or aliases can get past them.
- **It has been tried narrowly.** Claude Code is the only agent supported, macOS the only system tested, the thresholds have not been calibrated on real use, and the shared daemon has not yet run across a real network.

[How it decides](guide/how-it-works.md#known-limits) has the full list.

## This repository

This is the open core: the hook, the rules, the scope policy, the daemon and console, the activity report, unattended runs and the team features. Two parts used in the experiments are not included: the model judge and the drift score. The monitor runs without them; [When there is no judge](guide/how-it-works.md#when-there-is-no-judge) says what that changes.

Released under the [MIT licence](LICENSE).
