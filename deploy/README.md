# Running the shared daemon in a container

For a team: one daemon that machines running agents send their actions to, and that reviewers open in a browser. The main README's "One daemon for a team" explains the two kinds of token; this is how to host it.

You need Docker with Compose. Commands are run from the repository's root.

## 1. Build the image

```bash
docker compose -f deploy/compose.yaml build
```

The image holds the monitor and nothing else. The daemon runs in it as an unprivileged user on a read-only filesystem, and keeps everything it owns in one volume mounted at `/data`: the policy, the hashes of the tokens, the webhooks and the audit log.

## 2. Create an administrator

The daemon refuses to start as a shared one until it has an administrator, so this comes first:

```bash
docker compose -f deploy/compose.yaml run --rm monitor team add-member <your name> --role admin
```

The token is printed once and only its hash is stored. Any `riskmon` command can be run this way, against the same volume, whether or not the daemon is up:

```bash
docker compose -f deploy/compose.yaml run --rm monitor team add-member <name> --role reviewer
docker compose -f deploy/compose.yaml run --rm monitor team add-machine <machine name>
docker compose -f deploy/compose.yaml run --rm monitor team list
docker compose -f deploy/compose.yaml run --rm monitor notify add ops --slack
docker compose -f deploy/compose.yaml run --rm monitor notify console-url https://monitor.example.internal
```

Changes to members, machines and webhooks take effect at once; the running daemon does not need a restart.

## 3. Start it, with TLS in front

Tokens must not cross a network unencrypted. The daemon in the container speaks plain HTTP, and Compose publishes it on the host's loopback address only (`127.0.0.1:8787`), so by itself it is reachable from that host and nowhere else. Other machines reach it through something that terminates TLS. There are two ways.

**The bundled proxy.** It obtains and renews a certificate by itself. The name must resolve to this host, and ports 80 and 443 must be reachable by the certificate's issuer:

```bash
RISKMON_DOMAIN=monitor.example.com docker compose -f deploy/compose.yaml --profile tls up -d
```

**A proxy you already run** (nginx, a cloud load balancer, a tunnel). Start the daemon alone and forward to `127.0.0.1:8787`, passing the `Authorization` header through and allowing requests to stay open for ten seconds or more, which waiting for a reviewer uses:

```bash
docker compose -f deploy/compose.yaml up -d
```

The daemon's log will warn that it is listening without TLS. Inside the container that is expected: the encryption is the proxy's job.

Check it:

```bash
docker compose -f deploy/compose.yaml ps        # the monitor should be "healthy"
curl https://monitor.example.com/healthz        # {"ok": true, "team": true}
```

## 4. Connect machines and people

On each machine that runs agents, with the monitor installed there:

```bash
riskmon join --server https://monitor.example.com
```

Reviewers open `https://monitor.example.com` and paste their member token.

## Keeping it

- **Back up the volume.** `riskmon_data` holds the audit log and the policy. The tokens cannot be recovered from it, only replaced, since it holds their hashes.
- **Upgrade** by pulling the repository, building again and running `up -d`. The volume is kept.
- **The policy** is edited on the console's Policy page by an administrator. Keys the page does not cover go in `/data/policy.json`; leave `port` alone there, since the container's health check and the published port expect 8787.
- **Stop** with `docker compose -f deploy/compose.yaml down`. Adding `-v` deletes the volume, and the audit log with it.

## What has and has not been tried

The image was built and run on one Mac with Docker Desktop: the daemon started, refused to start without an administrator, took actions from a hook on the host and served its console API. The bundled proxy was tried on the same machine with a certificate it issued itself for `localhost`. It has not been run on a server, with a public certificate, behind another proxy, or under load.
