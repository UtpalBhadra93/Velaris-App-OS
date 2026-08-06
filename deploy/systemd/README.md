# Velaris under systemd

Three units replacing the `nohup … &` launches in `start-velaris.sh`:

| Unit | Replaces | Listens |
|------|----------|---------|
| `velaris-engine.service` | Step 6 | `0.0.0.0:8100` |
| `velaris-case-service.service` | Step 7 | `127.0.0.1:8201` (nginx fronts it on 8200) |
| `velaris-studio.service` | Step 8 | `0.0.0.0:5173` |

## Why

The nohup model has three failure modes that bit us on 2026-07-27, when Studio
was found dead with no way to establish why:

1. **No supervision** — nothing restarted the process, so a crash meant an
   outage until a human noticed.
2. **No history** — `start-velaris.sh` redirects with `>`, so restarting to fix
   the outage *truncated the log that would have explained it*. The RCA was
   unclosable by then.
3. **No boot start** — a reboot left everything down.

systemd fixes all three: `Restart=always`, journald (which survives restarts and
is timestamped), and `WantedBy=multi-user.target`.

## Install

On the server, from the Velaris directory:

```bash
sudo ./deploy/systemd/install.sh
./stop-velaris.sh          # clear the old nohup'd processes
sudo systemctl start velaris-engine velaris-case-service velaris-studio
```

`install.sh` substitutes the invoking user, the checkout path, and the absolute
`uv`/`npm` paths (systemd has no shell profile, so a version-managed `npm` is
otherwise not found).

## Operating

```bash
systemctl status velaris-studio
journalctl -u velaris-studio -f                  # live
journalctl -u velaris-studio --since "1 hour ago"
journalctl -u velaris-studio -p err --since today
sudo systemctl restart velaris-case-service
```

To answer "why did it die", which is what started this:

```bash
journalctl -u velaris-studio | grep -iE "oom|killed|heap|exit"
systemctl show velaris-studio -p NRestarts -p ExecMainStatus
```

## Deliberate choices

- **`--reload` dropped** from both uvicorn units. It forks a file-watching
  supervisor that confuses systemd's process tracking and would report a crash
  as a reload. Deploys restart the unit instead.
- **`StartLimitIntervalSec=0`** — never stop retrying. The default gives up
  after 5 restarts in 10s, which is exactly when you need it to keep trying.
- **`KillSignal=SIGINT`** on the uvicorn units — uvicorn drains connections on
  SIGINT; SIGTERM cuts them off.
- **`MemoryAccounting=yes`**, plus a 2 GB node heap cap on Studio. If the
  2026-07-27 outage was an OOM (never proven — `dmesg` had aged out), this makes
  the next one say so explicitly rather than vanishing.
- **No `PrivateTmp`** — the recording/egress paths use real `/tmp` locations.

## The Studio unit runs a dev server

`npm run dev` is Vite's development server: unminified source, an
unauthenticated HMR websocket, and a proxy block that forwards `/api/v1/*`
straight to case-service. That is what `start-velaris.sh` does today and the
unit matches it, but on an internet-reachable host the supported shape is
`npm run build` served by nginx on 443 with `/api/v1` proxied to
`127.0.0.1:8201`. The unit carries a commented `vite preview` ExecStart as the
first step in that direction.

## Risk: what this does and does not touch

Installing is **non-destructive and reversible**. It writes three files to
`/etc/systemd/system/`, and touches nothing else — not `.env`, not the database,
not the repo (beyond `chmod +x` on the wrapper). `systemctl enable` does **not**
start anything; until you `start` or reboot, the system behaves exactly as now.

To undo completely:

```bash
sudo systemctl disable --now velaris-engine velaris-case-service velaris-studio
sudo rm /etc/systemd/system/velaris-{engine,case-service,studio}.service
sudo systemctl daemon-reload
```

### Three real caveats

1. **The units do NOT run migrations or `npm install`.** `start-velaris.sh` does
   both. After an update that adds a migration or a dependency, a bare
   `systemctl restart` would run the app against an unmigrated database or a
   stale `node_modules`. Keep using `update-velaris.sh` for updates, then
   `systemctl restart`, rather than restarting alone.
2. **The database is waited for, not merely ordered after.** `After=docker.service`
   orders startup but only guarantees the *daemon* — not that Postgres inside a
   container has bound its port. The engine and case-service units therefore run
   `velaris-wait-deps.sh` as `ExecStartPre=`, which blocks until the DB port from
   `VELARIS_CASE_DATABASE_URL` accepts a connection (90s, then fails the unit so
   `Restart=always` retries). Temporal is waited for too, but softly — 30s, then
   it starts anyway and lets the poller retry.

   This is not theoretical. On **2026-08-05** a Docker daemon restart brought
   back only the five containers that had `restart: unless-stopped`; the
   database, cache, broker and vault stayed `Exited(255)` while case-service
   came up healthy and answered every login with a `500` for 30 minutes. Two
   fixes landed together: every long-running service in `docker-compose.yml` now
   carries `restart: unless-stopped`, and the units `Require=docker.service`
   plus the `ExecStartPre` wait above. `install.sh` also enables `docker.service`
   at boot, without which neither fix fires.
3. **`DATABASE_BACKEND` is not in `.env`** — `start-velaris.sh` derives it from
   `velaris.yaml`, and `case_service/config.py` silently defaults to
   `postgresql` when it is missing. `velaris-exec.sh` re-derives it at every
   start (along with `VELARIS_SERVICE_REGISTRY`) so the units and the script agree.
   Without that wrapper, a MySQL/MariaDB install would have run the Postgres
   code paths.

## Interaction with start-velaris.sh

`start-velaris.sh` still launches its own `nohup` copies. **Use one or the
other** — running both leaves two of each process fighting for the same port.
Once the units are in, use `systemctl` and keep the script for the database,
migrations, and dependency steps it also performs.
