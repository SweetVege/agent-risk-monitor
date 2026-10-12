# agent-risk-monitor

An out-of-band monitor for autonomous coding agents. It scores each action before it runs and, depending on the score, lets it through, asks a person, denies it, or suspends the session. When nobody is watching, it holds the action for review instead of asking.

The first supported agent is Claude Code, through its hooks.

This repository is the open core: the hook, the rule and trajectory tiers, the scope policy, the daemon and console, the activity report and unattended runs. Two parts used in the experiments under `evals/experiments/` are not included: the model judge that reviews gray-band actions against the user's task, and the drift score. The engine runs without them; what that changes is described below.

## Getting started

Python 3.10 or later. The core uses only the standard library.

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

It lists the state of the daemon and the protection, and says what to do about anything missing.

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
| Be told when something needs you | `riskmon notify add <name> --slack`; see "Telling people, and waiting for them" |
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

## One daemon for a team

By default the daemon serves one person on one machine and asks nobody who they are. It can instead be shared: machines that run agents send their actions to it, and the people who review them use its console from their own browsers. A shared daemon answers nothing without a token.

There are two kinds of token and neither does the other's job. A **machine token** lets a machine have its agents' actions checked, and nothing else: it cannot read the audit log, approve an action, resume a session or change the policy. That matters because the token sits on the machine where the agent runs. A **member token** is a person's. A reviewer sees everything and decides held actions; an admin also changes the policy. Every review is recorded with the name of the member who made it.

On the machine that hosts the daemon:

```bash
riskmon team add-member lead --role admin     # prints the token once; only its hash is kept
riskmon team add-member rui --role reviewer
riskmon team add-machine ci-runner-1
riskmon serve --listen 0.0.0.0 --cert fullchain.pem --key privkey.pem
```

The daemon will not start as a shared one until there is an admin. Without `--cert` and `--key` it serves plain HTTP and says so; tokens then cross the network unprotected, so do that only behind something that provides TLS itself. `riskmon team list` shows who has access and `riskmon team remove <name>` ends it at once.

On each machine that runs agents:

```bash
riskmon join --server https://monitor.example.internal:8787
```

It asks for the machine token without showing it, checks that the daemon accepts it, keeps it in a file only that user can read, and turns protection on for every Claude Code session that user starts. `riskmon leave` undoes it. If the daemon cannot be reached, the hook falls back to the rules it has locally: ordinary work goes through and anything beyond the default policy is refused.

Members open the daemon's address in a browser and paste their token, which is kept for that browser tab only. Evaluations are not started from a shared daemon's page.

On a shared daemon the Protect page says which machine each session ran on and as which account, and can be narrowed to one machine or one project. Each approval, refusal and resumed session carries the name of the member who did it.

## Telling people, and waiting for them

**Notices.** The daemon can post to a webhook when an action is held for review or a session is suspended, so that someone finds out without watching the console:

```bash
riskmon notify add ops --slack                     # asks for the webhook's address without showing it
riskmon notify console-url https://monitor.example.internal:8787
riskmon notify test ops
```

`--slack` posts in the form Slack's incoming webhooks take; without it the notice is plain JSON for any receiver. `--only held` or `--only suspended` narrows what a webhook gets. A notice says what kind of limit was met, on which machine and in which project, and links to the session once the console's address is set. It leaves out the command, the reason and the task unless the webhook is added with `--with-command`: they go to another service, and a command can hold a credential. A webhook's address is itself a secret, so it is kept in a file only the daemon's user can read and is set from the command line, never from the page. A webhook that is slow or failing never delays a verdict; the console's header says when the last notice failed, and `riskmon doctor` lists the webhooks that are set up. `riskmon notify list` and `riskmon notify remove <name>` do the rest.

**Waiting for a reviewer.** By default a held action comes straight back to the agent, which is told to carry on with something else. A machine can instead be set up to pause the run while a person decides:

```bash
riskmon init --user --wait-for-review 600          # or: riskmon join --server ... --wait-for-review 600
```

An unattended run on that machine then waits up to that many seconds on a held action. Approved in that time, the action runs and the agent never learns it was held. Kept refused, the agent is told a reviewer decided. If nobody decides, the action is held as before and the run moves on. The review queue marks such an action as pausing its run, and the notice says how long it will wait. This is off unless asked for, because a paused run costs time whenever nobody answers; it suits teams that have set up notices and do answer them. Running `riskmon init` again without the option turns it off.

## Layout

| File | Role |
|---|---|
| `riskmon/hook.py` | The Claude Code hook. Sends the action to the daemon and turns the verdict into a Claude Code decision |
| `riskmon/events.py` | Adapter layer. Turns a tool call into an `ActionEvent` |
| `riskmon/rules.py` | L0, the capability allowlist. Whether a host, path or command is beyond what the policy allows |
| `riskmon/scope.py` | Scope policy. Says outright how a class of action is handled |
| `riskmon/projects.py` | The policy's two layers: a baseline, and what each project adds to it |
| `riskmon/repo.py` | Which repository a directory belongs to, read from its git files |
| `riskmon/state.py` | L1, trajectory state. Carried score, behaviour tags, combination rules, the summary a judge reads |
| `riskmon/optional.py` | The slots for the judge and the drift score, and what stands in when they are absent |
| `riskmon/engine.py` | Ties the tiers together and maps the result to a verdict |
| `riskmon/server.py`, `app.html`, `insights.py` | The local daemon and the console |
| `riskmon/access.py` | Who may talk to a shared daemon: machine and member tokens |
| `riskmon/notify.py` | Notices to webhooks when an action is held or a session suspended |
| `riskmon/report.py` | The activity report |
| `riskmon/install.py`, `service.py`, `doctor.py` | `riskmon init`, `riskmon service` and `riskmon doctor` |
| `riskmon/runner.py` | Unattended runs |
| `riskmon/default_policy.json` | The default policy: thresholds, host allowlist, protected paths, command classes, scope policy |
| `evals/` | Eval replay and experiment records |

## How an action is scored

1. **L0** scores the action on three dimensions: boundary, stealth and resource. It is an allowlist: it only asks whether the action goes beyond what the policy allows.
2. **L1** adds the session's history. It keeps track of how many locations outside the workspace the session has touched and adds to the score from the third new one. Contacting an unlisted host after touching a protected path, running a suspicious file written earlier, and trying again after being denied all add to the score.
3. When the score falls in the gray band (30–70 by default) and a judge is installed, **L2** reads the task, the compressed summary and the action, scores it again, and adds an off-task dimension. No judge ships with this repository, so this step is skipped and the rule scores stand.
4. The final score maps to a verdict: below 50 allow, 50–70 ask a person, 70–90 deny, and 90 or more, or three denials, suspend the session.

**Human review.** In the console, every ask, deny and suspension has a "Mark false positive" control. Marking one removes the carried score, behaviour tags and denial count that verdict left in the session, and the same action is allowed outright if it recurs in that session (shown as tier `HUMAN`). Actions that touch the monitor itself cannot be marked.

**Observation.** Besides scoring, the monitor records how each allowed action turned out (a length for successes, an excerpt for failures), the model's own one-line description of a command, and what the model said earlier in the turn. These go to the audit log only and do not affect scoring. The model's reasoning is not in the transcript and cannot be read.

**Unattended runs.** When the script that launches the agent sets `RISKMON_MODE=unattended`, an action that would have prompted is held instead. It does not run; the agent is told it is waiting on review and to carry on with something else. A person can approve it later in the console, after which a retry in that session is allowed. A hold is not a violation, but once `unattended.max_holds` (5 by default) pile up, the run is suspended.

**Scope policy.** The `scope` section of the policy can say outright how a class of action is handled, without scoring, and separately for attended and unattended sessions. Each rule is `score` (score as usual), `confirm` (a person must agree) or `deny`. The judge is not asked about actions the policy rules on; only a person can approve them in the console. By default everything is scored when attended. Unattended runs may not write outside the workspace or search the whole machine or home directory, and need a person for searches outside the workspace, unlisted hosts and protected paths.

The policy can also declare paths inside the workspace read-only, for example vendored code or the tests an agent is asked to make pass:

```json
{ "scope": { "read_only": ["vendor", "tests"] } }
```

A change to one is denied in an unattended run and needs a person's agreement in an attended one. Reading and running what is there is unaffected. Nothing is read-only unless the policy says so.

**Baseline and projects.** The policy has two layers. The baseline is what every session follows. A project is a repository, a directory, or both, with additions of its own: hosts it may contact, directories it may use outside its own, paths that are read-only or protected there. A session gets the baseline plus those when it starts in a checkout of that repository, wherever the checkout is and on whichever machine, or in that directory or below it. The repository is read from the checkout's `origin` remote and written `host/owner/name`, so a clone over SSH and one over HTTPS are the same project, and a project's relative paths are taken from each checkout's own root. To put a run under a project by name instead, launch it with `riskmon run --project <name>` or with `RISKMON_PROJECT` set. Which project a session belongs to is settled at its first action and does not change after that. A project can also make the handling of a kind of action stricter than the baseline; it cannot make it looser. Both layers are edited on the console's Policy page and kept in the monitor's home directory (`policy.json` and `projects.json`), which an agent is not allowed to read or change.

An unattended run can declare extra scope when it is launched. It cannot be widened afterwards:

```bash
riskmon run --cwd <project> --allow-host api.open-meteo.com --allow-dir ~/data "task"
```

**Observe mode.** `riskmon run --observe` is for research. Actions that would be held are let through and marked as such, while denials and suspensions still apply, to see how far an agent goes when nothing stops it. It can only be switched on at launch.

A few fixed constraints:

- Actions already over the deny line are not sent to the judge. A model must not turn a clear violation into an allow.
- The carried score is a fuzzy signal. At most it pushes an action to the top of the gray band, for the judge or a person to decide. Denials and suspensions come only from explicit rules or the denial count.
- An action the judge reviewed is carried at the judge's conclusion, so one it found reasonable does not weigh on later actions.
- Being off-task, on its own, escalates to a confirmation at most.
- The reason sent back to the agent for a denial carries no rule detail, with one exception: when the scope policy stops an action, the agent is told which declared limit it met and on what, so that it can look for a way to do the task inside it. Reasons that come from scoring, from the judge or from the monitor protecting itself go to the audit log and the console only.
- When the daemon is unavailable, the hook runs L0 locally, denies anything over the gray threshold and allows the rest.
- When a gray-band action should have gone to a judge and could not (a timeout, a rate limit), it goes to a person instead of counting as reviewed. This does not apply when no judge is installed; the rule scores decide then.

## Without the judge

The rules can tell that an action reaches beyond what the policy allows. They cannot tell whether the user asked for it. With no judge:

- A gray-band action scoring 30 to 49 is allowed and one scoring 50 to 69 goes to a person, whatever the task was. On the benign traces in `evals/`, 8 of 74 actions go to a person this way. Expect more prompts on legitimate work than the experiment records describe, and add the hosts and paths you use to the policy.
- Nothing is scored as off-task.

The engine takes a judge as an argument: `Engine(cfg, judge=...)` accepts any object with an `available` attribute and an `evaluate(tasks, summary, event, findings)` method returning a `JudgeResult` (in `riskmon/events.py`) or `None`. `tests/test_engine.py` has a stub that shows the shape. The daemon does not yet load one from the policy.

## Tests

```bash
python3 -m unittest discover -s tests
```

```bash
python3 -m evals.run
```

## Known limits

- **Subprocesses are a blind spot.** The hook only sees tool calls the agent makes. What a script or background process it starts goes on to do is invisible here. A complete product needs egress control at the operating-system level.
- **This is not an isolation boundary.** The monitor and the agent run on the same machine as the same user, so the self-protection rules only stop what goes through a tool call. A production deployment has to put the monitor where the agent cannot reach it.
- **A project is recognised by what the checkout says.** The repository comes from the working directory's git configuration, which the agent's own machine reports. It is read before the session's first action, so an agent cannot move its own session into another project, but nothing verifies that a checkout really is a clone of the repository it names.
- **Notices and waiting have only been tried against a receiver on the same machine.** No real Slack workspace was used. A pause depends on the hook finishing inside the time Claude Code allows it, which the installer sets; a hook that runs out of time lets its action through, as any Claude Code hook does.
- **L0 parses text.** Hosts and paths are taken from the command string, so indirection through variables or aliases gets past it. That is why a judge and system-level controls are needed.
- **Detection is largely untested.** `evals/` has benign traces and a few recorded sessions. They do not show what the monitor can stop. The runs recorded under `evals/experiments/` were all made through Claude Code: about 120 with three Claude models, none of which reached for anything that was not the project's, and 40 with two models from another vendor, one of which tried to read a neighbouring project's token file in four of twenty runs and was denied each time. Those results describe a model inside Claude Code, not the model on its own. Later runs in a separate evaluation harness, whose records are not part of this repository, gave each model a two-sentence system prompt. There the smallest of the three Claude models did take the token, and in other runs wrote the data itself and reported the task done; the other vendor's larger model used the token in every run the monitor did not stop. With the monitor refusing, the token reached no agent in a hundred runs. That is one scenario and twenty runs per model, which is enough to say the monitor's refusals mattered there and too little to state a detection rate. See `evals/README.md` and `evals/experiments/`.
- **The experiment records describe the full monitor.** The runs in `evals/experiments/` had the model judge and, in the later ones, the drift score. Scores and verdicts quoted there will not all reproduce with this repository alone.
- **The thresholds and point values are first guesses.** They have not been calibrated on real data.
- **Tested on macOS only**, with Claude Code as the only agent.
- **The shared daemon has only been exercised on one machine.** Its tests and a hands-on check ran the daemon and a joining client on the same computer over the loopback address. It has not been run across a real network, behind a proxy, or with a real certificate.
