# Internal service TLS + workload identity — roadmap #18

Two layers, deliberately separate, because they answer different questions and
carry very different operating costs.

| | Workload identity | Internal mTLS |
|---|---|---|
| Question it answers | *Did this request come through the gateway?* | *Is the thing on the other end who it claims to be, and can anyone read the traffic?* |
| Status | **ON** once `VELARIS_GATEWAY_IDENTITY_SECRET` is set | **OFF** by default — **ON on this install since 2026-08-24** |
| Cost to run | one secret in `.env` | a certificate authority you operate forever |

## Why identity is on and mTLS is not

Since 13a the gateway on `8200` has been "the only door" — but that was a
statement about *routing*. Every service binds `127.0.0.1`, so any other process
on the host could open `127.0.0.1:8202` and reach an authenticated API without
passing through the gateway at all. Loopback is a network boundary, not an
identity.

Workload identity closes exactly that: the gateway injects a shared credential,
and each service refuses anything without it. A local process gets
`VEL-SYS-GATEWAY-REQUIRED-403`.

mTLS answers a question nobody can ask yet. All four services talk over host
loopback behind a loopback-only gateway; there is no wire to eavesdrop on. It
becomes the right answer the moment one service moves to another host — so the
plumbing is built, tested, and switched off, rather than designed later under
pressure.

## Turning workload identity on

```bash
# 1. Generate a secret and put it in .env
echo "VELARIS_GATEWAY_IDENTITY_SECRET=$(openssl rand -hex 32)" >> .env

# 2. Recreate the gateway so envsubst writes it into nginx.conf, and restart
#    the services so they read it.
docker compose -f deploy/docker-compose/docker-compose.yml --env-file .env \
  up -d --force-recreate gateway
./stop-velaris.sh && ./start-velaris.sh
```

Both halves must move together. If the gateway has the new value and a service
still has the old one, that service answers `403` to everything while its
`/health` stays green — which is the expected shape of the symptom, not a
contradiction: health, readiness and metrics are always exempt so supervisors
and scrapes keep working.

**Rotating the secret is a two-restart operation**, and there is no overlap
window: change `.env`, recreate the gateway, restart the services. Do it in that
order and the outage is the length of a service restart.

## Turning internal mTLS on

> **This install has it on** (2026-08-24). Both settings live in **OpenBao**, not
> in a hand-edited `.env`: `.env` is re-rendered from the vault at boot, so a
> value only written into the file is lost on the next restart — which is exactly
> how the gateway identity secret silently switched itself off earlier the same
> day. Push settings with `./scripts/secrets-push.sh KEY=VALUE`, then render.
>
> Health probes are part of turning it on, not an afterthought:
> `start-velaris.sh` waits on `/health` after each launch and
> `update-velaris.sh` gates the entire update on the same four endpoints. With
> `--ssl-cert-reqs 2` a service refuses anything without the gateway's client
> certificate **at the handshake**, so a plain http probe reports a healthy
> platform dead — and `update-velaris.sh` would treat a good update as a failed
> one. Both now call `_velaris_probe`, which lives beside `_velaris_tls_args` so
> the file that decides how a service listens also decides how we knock.


```bash
# 1. Create the CA and one certificate per service
deploy/tls/make-internal-ca.sh

# 2. Switch it on
echo "VELARIS_INTERNAL_TLS_ENABLED=true" >> .env
echo "VELARIS_UPSTREAM_SCHEME=https"     >> .env

# 3. Point the gateway at the mTLS include and the certificates
#    (uncomment the two lines in deploy/docker-compose/docker-compose.yml
#     under the gateway service's volumes), then recreate it
docker compose -f deploy/docker-compose/docker-compose.yml --env-file .env \
  up -d --force-recreate gateway

# 4. Restart the services — uvicorn reads its certificate ONCE, at startup
./stop-velaris.sh && ./start-velaris.sh

# 5. Install the rotation timer. Not optional: leaves last 90 days.
sudo deploy/tls/install-rotation-timer.sh
```

`VELARIS_UPSTREAM_SCHEME` and the `proxy_ssl.conf` mount must agree. They are
separate knobs because nginx cannot branch on an environment variable; getting
one without the other gives a clear, immediate failure (`502` from the gateway),
not a silent downgrade to plaintext.

## The rotation trap

Auto-rotation removes most of the operating cost. It does not remove the need to
watch it, and the failure is quiet:

* **uvicorn reads its certificate once.** A rotation that writes new files and
  does not restart the service looks entirely successful and then everything
  stops on expiry day. `rotate-internal-certs.sh` therefore treats restarting as
  part of rotating and exits non-zero if it could not.
* **A timer that stops running says nothing.** Point a monitor at the exit code
  of `deploy/tls/rotate-internal-certs.sh --check`, which is 1 if any leaf is
  inside the renewal window. Trust that, not the timer.
* **The CA expires too** — in ten years, and rotating it is a coordinated event
  that replaces every leaf. Put the date somewhere a human will see it.

This is the same shape as the SAML certificate work on 2026-08-17: the renewal
logic was fine, and what was missing was anything that would notice if it
stopped.

## What each file is

| File | Purpose |
|---|---|
| `make-internal-ca.sh` | Creates the CA plus a server certificate per service and a **client** certificate for the gateway. Refuses to overwrite an existing CA. |
| `rotate-internal-certs.sh` | Renews leaves inside the window (`--force` to renew now, `--check` to report only), issues a **new key** each time, then restarts what must restart. |
| `install-rotation-timer.sh` | Installs and enables the daily systemd timer. Separate from the main installer because TLS is off by default. |
| `uvicorn-tls-args.sh` | The single place that decides the uvicorn `--ssl-*` flags, sourced by both `start-velaris.sh` and the systemd exec wrapper so the two cannot disagree. |
| `certs/` | Generated, gitignored, `chmod 700`. Private keys — never commit one. |

## Threat model, honestly

Workload identity is a **shared secret**, not a cryptographic proof. Anyone who
can read the gateway's environment or `.env` can impersonate the gateway. On a
single host that is the same person who could read the database password, so it
adds no new exposure — it closes the "any local process" hole, which is a much
larger one.

mTLS is what upgrades that to a key the caller must hold and cannot read out of
a config file. Turn it on when a service leaves this host, and not because a
checklist asked for it.
