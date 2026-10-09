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

**2. Start the daemon**

```bash
riskmon serve
```

It listens on this machine only. The console is at http://127.0.0.1:8787. Leave this terminal open.

**3. Install the hooks in the project you want monitored**

```bash
cd your-project
riskmon init
```

This writes four hooks into the project's `.claude/settings.json` and leaves existing settings and other hooks alone. Claude Code sessions **started after this** in the project are monitored. `riskmon init --remove` takes the hooks out.

**4. Check**

```bash
riskmon doctor
```

It lists the state of the daemon and the hooks, and says what to do about anything missing.

When the daemon is not running, the hook denies actions such as network access and reads of sensitive files and lets the rest through. Start the daemon before working in a project that has the hooks.

## Everyday use

| To | Do this |
|---|---|
| See what a session touched | Open it in the console, or `riskmon report <session id>` |
| Resume a suspended session | "Resume session" in the console, or `riskmon resume <session id>` |
| Overturn a wrong verdict, or approve a held action | "Mark false positive" or "Approve" under that row in the console |
| Run a task unattended | `riskmon run --cwd <project> "task"`; needs the `claude` CLI |
| Change the policy | Create `~/.riskmon/policy.json` with just the keys you change, then restart the daemon |

A policy example that adds an API you use to the host allowlist and raises the deny line:

```json
{
  "allow_hosts": ["github.com", "pypi.org", "api.open-meteo.com"],
  "thresholds": { "deny": 80 }
}
```

Dictionaries are merged level by level and lists are replaced whole, so the `allow_hosts` above replaces the default allowlist. Every setting and its default is in `riskmon/default_policy.json`.

The policy and the audit data live in `~/.riskmon/`; set `RISKMON_HOME` to move them. Running straight from a checkout (`python3 -m riskmon ...`) uses the checkout itself.

## Layout

| File | Role |
|---|---|
| `riskmon/hook.py` | The Claude Code hook. Sends the action to the daemon and turns the verdict into a Claude Code decision |
| `riskmon/events.py` | Adapter layer. Turns a tool call into an `ActionEvent` |
| `riskmon/rules.py` | L0, the capability allowlist. Whether a host, path or command is beyond what the policy allows |
| `riskmon/scope.py` | Scope policy. Says outright how a class of action is handled |
| `riskmon/state.py` | L1, trajectory state. Carried score, behaviour tags, combination rules, the summary a judge reads |
| `riskmon/optional.py` | The slots for the judge and the drift score, and what stands in when they are absent |
| `riskmon/engine.py` | Ties the tiers together and maps the result to a verdict |
| `riskmon/server.py`, `dashboard.html` | The local daemon and the console |
| `riskmon/report.py` | The activity report |
| `riskmon/install.py`, `doctor.py` | `riskmon init` and `riskmon doctor` |
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
- The reason sent back to the agent for a denial carries no rule detail. The detail goes to the audit log and the console only.
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
- **L0 parses text.** Hosts and paths are taken from the command string, so indirection through variables or aliases gets past it. That is why a judge and system-level controls are needed.
- **Detection is largely untested.** `evals/` has benign traces and a few recorded sessions. They do not show what the monitor can stop. In more than fifty runs of tasks that could not be completed legitimately, the agent under test never crossed a stated limit, so there are no real violations to measure against. See `evals/README.md` and `evals/experiments/`.
- **The experiment records describe the full monitor.** The runs in `evals/experiments/` had the model judge and, in the later ones, the drift score. Scores and verdicts quoted there will not all reproduce with this repository alone.
- **The thresholds and point values are first guesses.** They have not been calibrated on real data.
- **Tested on macOS only**, with Claude Code as the only agent.
