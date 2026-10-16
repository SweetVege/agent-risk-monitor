# Running it for a team

One daemon that every machine running agents sends its actions to, and that the people who review them open in a browser. To host it in a container, with TLS in front of it, see [`deploy/README.md`](../deploy/README.md).

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

The policy is the daemon's, and it is applied where the agent is. `~` in a protected path means the home directory on the machine the session runs on, not the daemon's, and that machine's copy of the monitor and the token it joined with are kept from its agents like the daemon's own files.

To host the shared daemon in a container, with TLS in front of it, see [`deploy/README.md`](../deploy/README.md).

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
