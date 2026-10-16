# How it decides

What the monitor checks for each action, how a verdict is reached, and what it cannot see. For installing and using it, see [Using it](usage.md).

## How an action is scored

1. **L0** scores the action on three dimensions: boundary, stealth and resource. It is an allowlist: it only asks whether the action goes beyond what the policy allows.
2. **L1** adds the session's history. It keeps track of how many locations outside the workspace the session has touched and adds to the score from the third new one. Contacting an unlisted host after touching a protected path, running a suspicious file written earlier, and trying again after being denied all add to the score.
3. When the score falls in the gray band (30–70 by default) and a model judge is installed and available, **L2** reads the task, the compressed summary and the action, scores it again, and adds an off-task dimension. Without one, this step is skipped and the rule scores stand.
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
- When a gray-band action should have gone to the judge and could not (a timeout, a rate limit), it goes to a person instead of counting as reviewed. This does not apply when there is no judge, or it is switched off or has no credentials; the rule scores decide then.


## When there is no judge

The rules can tell that an action reaches beyond what the policy allows. They cannot tell whether the user asked for it. With no judge:

- A gray-band action scoring 30 to 49 is allowed and one scoring 50 to 69 goes to a person, whatever the task was. On the benign traces in `evals/`, 8 of 74 actions go to a person this way. Expect more prompts on legitimate work than the experiment records describe, and add the hosts and paths you use to the policy.
- Nothing is scored as off-task.

The engine takes a judge as an argument: `Engine(cfg, judge=...)` accepts any object with an `available` attribute and an `evaluate(tasks, summary, event, findings)` method returning a `JudgeResult` (in `riskmon/events.py`) or `None`. `tests/test_engine.py` has a stub that shows the shape. The daemon does not load another judge from the policy yet.

## Known limits

- **Subprocesses are a blind spot.** The hook only sees tool calls the agent makes. What a script or background process it starts goes on to do is invisible here. A complete product needs egress control at the operating-system level.
- **This is not an isolation boundary.** The monitor and the agent run on the same machine as the same user, so the self-protection rules only stop what goes through a tool call. A production deployment has to put the monitor where the agent cannot reach it.
- **A project is recognised by what the checkout says.** The repository comes from the working directory's git configuration, which the agent's own machine reports. It is read before the session's first action, so an agent cannot move its own session into another project, but nothing verifies that a checkout really is a clone of the repository it names.
- **Notices and waiting have only been tried against a receiver on the same machine.** No real Slack workspace was used. A pause depends on the hook finishing inside the time Claude Code allows it, which the installer sets; a hook that runs out of time lets its action through, as any Claude Code hook does.
- **L0 parses text.** Hosts and paths are taken from the command string, so indirection through variables or aliases gets past it. That is why a judge and system-level controls are needed.
- **Detection is largely untested.** `evals/` has benign traces and a few recorded sessions; they do not show what the monitor can stop. The runs recorded under `evals/experiments/` were all made through Claude Code: about 120 with three Claude models, none of which reached for anything that was not the project's, and 40 with two models from another vendor, one of which tried to read a neighbouring project's token file in four of twenty runs and was denied each time. Those results describe a model inside Claude Code, not the model on its own. Later runs in an evaluation harness, which can be replayed on the [project's page](https://sweetvege.github.io/agent-risk-monitor/), gave each model a two-sentence system prompt. There the smallest of the three Claude models did take the token, and in other runs wrote the data itself and reported the task done; the other vendor's larger model used the token in every run the monitor did not stop. With the monitor refusing, the token reached no agent in a hundred runs. That is one scenario and twenty runs per model, which is enough to say the monitor's refusals mattered there and too little to state a detection rate. See `evals/README.md` and `evals/experiments/`.
- **Some experiment records rely on parts the open edition does not have.** The runs in `evals/experiments/` had the model judge and, in the later ones, the drift score. With the open edition alone, not every score and verdict quoted there reproduces.
- **The judge is inconsistent across similar actions.** With temperature 0 the same input gets the same score, but differently worded commands doing the same thing can land on either side of a threshold.
- **The thresholds and point values are first guesses.** They have not been calibrated on real data.
- **Tested on macOS only**, with Claude Code as the only agent.
- **The shared daemon has only been exercised on one machine.** Its tests and hands-on checks ran the daemon and a joining client on the same computer, the daemon both directly and in a container. It has not been run across a real network or with a publicly issued certificate.

## Layout

| File | Role |
|---|---|
| `riskmon/hook.py` | The Claude Code hook. Sends the action to the daemon and turns the verdict into a Claude Code decision |
| `riskmon/events.py` | Adapter layer. Turns a tool call into an `ActionEvent` |
| `riskmon/rules.py` | L0, the capability allowlist. Whether a host, path or command is beyond what the policy allows |
| `riskmon/scope.py` | Scope policy. Says outright how a class of action is handled |
| `riskmon/projects.py` | The policy's two layers: a baseline, and what each project adds to it |
| `riskmon/repo.py` | Which repository a directory belongs to, read from its git files |
| `riskmon/state.py` | L1, trajectory state. Carried score, behaviour tags, combination rules, the summary L2 reads |
| `riskmon/optional.py` | Loads the judge and the drift score if they are installed, and stands in for them if not |
| `riskmon/engine.py` | Ties the tiers together and maps the result to a verdict |
| `riskmon/server.py`, `app.html`, `insights.py` | The local daemon and the console |
| `riskmon/access.py` | Who may talk to a shared daemon: machine and member tokens |
| `riskmon/notify.py` | Notices to webhooks when an action is held or a session suspended |
| `riskmon/report.py` | The activity report |
| `riskmon/install.py`, `service.py`, `doctor.py` | `riskmon init`, `riskmon service` and `riskmon doctor` |
| `riskmon/runner.py` | Unattended runs |
| `riskmon/default_policy.json` | The default policy: thresholds, host allowlist, protected paths, command classes, scope policy |
| `docs/` | The project's page, served by GitHub Pages: recorded evaluation runs, and a demo of the console with sample data |
| `deploy/` | The shared daemon as a container image, with a Compose file and an optional TLS proxy |
| `evals/` | Eval replay and experiment records |

## Tests

```bash
python3 -m unittest discover -s tests
```

```bash
python3 -m evals.run
```
