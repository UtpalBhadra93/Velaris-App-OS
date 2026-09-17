# Velaris on Kubernetes — PostgreSQL

Running Velaris on Kubernetes with the chart at `deploy/helm/velaris`, plus the
three scripts in this directory. This directory covers the **PostgreSQL** track;
`K8/MySQL/` and `K8/MariaDB/` are its siblings and share this chart, these
scripts and these prerequisites — only the database subchart and the migration
track differ. This file is the fullest of the three: where the siblings say "as
PostgreSQL §N", the detail is here. Everything below was established by
installing the chart on a real cluster, loading it, and breaking it on purpose —
the numbers and failure modes are measured, not estimated.

**Verified on** kind v0.32.0 / Kubernetes v1.36.1, Helm v4.2.4, kubectl v1.36.4,
node capped at 8 CPU / 16 GB.

**Result: 58/58 functional checks green** on PostgreSQL 18.6 (Bitnami chart
18.8.13), re-verified from a cold start on **2026-08-26** at 8 CPU / 16 GB —
see §10. The 55/55 figures below are the 2026-08-25 runs, kept because the load
and failure numbers were measured then.

---

## 1. Scripts

| Script | Purpose |
|---|---|
| `setup-velaris-k8s.sh` | First install: chart, migrations, verification |
| `update-velaris-k8s.sh` | Move an existing install forward, with rollback |
| `stop-velaris-k8s.sh` | Stop / start / uninstall / purge |
| `_common.sh` | Shared helpers — sourced, not run |

```bash
./K8/Postgres/setup-velaris-k8s.sh --build --kind my-cluster   # build images, load, install
./K8/Postgres/setup-velaris-k8s.sh --dry-run                   # render only
./K8/Postgres/update-velaris-k8s.sh --dry-run                  # show pending migrations, change nothing
./K8/Postgres/update-velaris-k8s.sh                            # migrate, upgrade, verify, roll back on failure
./K8/Postgres/stop-velaris-k8s.sh                              # scale to zero, keep all data
./K8/Postgres/stop-velaris-k8s.sh --start                      # resume
./K8/Postgres/stop-velaris-k8s.sh --uninstall                  # remove release, keep PVCs
./K8/Postgres/stop-velaris-k8s.sh --purge                      # DELETES THE DATABASE
```

Environment: `VELARIS_K8S_NAMESPACE` (default `velaris`), `VELARIS_K8S_RELEASE`
(default `velaris`), `VELARIS_CHART_DIR`, `VELARIS_IMAGE_REGISTRY`,
`VELARIS_IMAGE_TAG`, `VELARIS_K8S_ADOPT`.

### What the update script guarantees

Someone is already running the platform, so an update that changes nothing for
them must break nothing for them:

- **values are reused** (`--reuse-values`) — a setting an operator applied by
  hand is never re-derived or dropped;
- **nothing is enabled** that was not already enabled;
- **live replica counts are pinned** into the upgrade, so it neither fights the
  autoscaler nor resets a deployment somebody scaled by hand;
- **migrations run before the new code**, against the version still serving
  traffic, and only those not yet recorded;
- **a failed rollout rolls back** to the last revision helm actually had
  deployed — and says so plainly when there is nothing to roll back.

### The migration runner

Applied files are recorded in `schema_migrations`, the same ledger the host
installer uses. Two deliberate differences:

- **`ON_ERROR_STOP=1`.** A migration that fails stops the run and is *not*
  recorded. The host installer pipes migrations through `psql` without it, so a
  file that fails half way exits 0, gets recorded as applied, and setup prints a
  tick over a schema that never changed (see §4).
- **Adoption guard.** These migrations are not re-runnable. If the database
  already carries the schema but the ledger accounts for less than half the
  migrations — an install migrated by the host installer, restored from a dump,
  or migrated by hand — the runner refuses rather than replaying 001 onward. Run
  `VELARIS_K8S_ADOPT=1 ./K8/Postgres/update-velaris-k8s.sh` to record them as applied
  without executing them.

---


### Reusing an existing Velaris database

An earlier Velaris install being moved into Kubernetes, a restored dump, or a
database that lives outside the cluster. The bundled database is then not
installed at all:

```bash
VELARIS_K8S_EXTERNAL_URL="postgresql+asyncpg://user:pass@host:5432/velaris" \
  ./K8/Postgres/setup-velaris-k8s.sh
```

Prefer the environment variable over the `--external-url` flag so the password
does not land in your shell history. (The DSN is rendered into the deployment
environment either way, exactly as the bundled password is today — see the
security notes.)

What happens:

1. the connection is parsed and reported (host, port, database — never the password);
2. **the target is checked before anything is written.** An empty database is
   accepted; a database holding Velaris tables is recognised and adopted; a
   database holding *other* tables is **refused** — pointing an install at the
   wrong DSN and replaying migrations over somebody else's schema is the most
   destructive mistake available here, and it is a plausible typo;
3. only migrations not already in `schema_migrations` are applied, so an old
   install is brought up to the current version and nothing is replayed;
4. queries and migrations run from a short-lived client pod
   (`VELARIS_DB_CLIENT_IMAGE`), since there is no database pod to exec into.

**Verified:** a brand-new install with no database of its own, pointed at an
existing Velaris database, recognised it, applied 0 migrations, and scored
56/56 on the functional suite while serving that database's existing data —
logging in as the superadmin stored in it. The refusal path was verified too,
against a database holding two non-Velaris tables.

Not for importing *another product's* database — that is HxDBMigrate's job,
after setup, and it creates cases rather than replacing the schema.

---

## 2. Prerequisites

Six things, and the install fails without any one of them. The seventh is
optional and only costs you autoscaling.

| # | What | Why it is required |
|---|------|--------------------|
| 1 | **A reachable cluster with a default StorageClass** | The bundled database and Valkey each claim a PVC. With no default class they stay `Pending` and nothing ever starts. Any conformant Kubernetes: EKS, GKE, AKS, OpenShift, k3s, kind. |
| 2 | **`kubectl` and `helm` v3+** on the machine you run the script from — `docker` too, but only if you build images | `require_cluster` checks `kubectl cluster-info` and stops if nothing answers. |
| 3 | **A container registry the cluster can pull from**, named in `VELARIS_IMAGE_REGISTRY` | The cluster cannot see images built on your laptop. `kind load` is a kind-only shortcut; on a vendor cluster you build and push. |
| 4 | **Temporal, already running** | A hard dependency the chart does **not** deploy. The worker connects to `worker.temporalHost` and crash-loops without it. |
| 5 | **A superadmin, created after the migrations** | Until one exists every API route answers **503** (health probes excepted). |
| 6 | **An ingress controller and a TLS certificate**, if anyone outside the cluster is to reach it | The chart ships neither, on purpose. |
| — | *metrics-server (optional)* | Without it the chart's two HPAs report `<unknown>` and never scale. Replicas stay fixed; nothing else breaks. |

**Check which cluster you are about to install into before you start.** The
scripts install into whatever `kubectl config current-context` points at —
that, and nothing else, decides which cluster gets it.

```bash
kubectl config current-context
kubectl get storageclass          # one must be marked (default)
VELARIS_IMAGE_REGISTRY=your.registry/velaris ./K8/Postgres/setup-velaris-k8s.sh --build
```

**Tools:** `kubectl`, `helm` (v3+), and `docker` only if building images.

**Cluster:** any conformant Kubernetes. A default StorageClass is required —
the bundled PostgreSQL and Valkey both claim a PVC.

**metrics-server** is *not* a dependency of the chart but the chart ships two
HPAs. Without it they report `<unknown>` and never scale; replicas stay fixed.
Nothing else breaks.

**Choose your database engine by directory.** `K8/Postgres/`, `K8/MySQL/` and
`K8/MariaDB/` each install the same chart with that engine's subchart enabled and
its own migration track; all three are proven. *(This paragraph used to say the
chart was PostgreSQL-only — that stopped being true when the MySQL and MariaDB
subcharts landed.)*

**Temporal is a hard external dependency.** The chart does not deploy it. The
worker connects to `worker.temporalHost` (default
`temporal.default.svc.cluster.local:7233`) and crash-loops without it. Note that
Temporal 1.25 will not run against PostgreSQL 18 — it fails with
`database connection lost: EOF`, so give Temporal its own PostgreSQL 16.

**Images.** Five: `case-service`, `analytics-service`, `hxgraph-service`,
`hxmigrate-service`, `engine`. `worker` deliberately reuses the case-service
image with a different entrypoint.

**Bootstrap.** A superadmin must exist or every endpoint answers 503 (health
probes excepted). `scripts/setup_superadmin.py` is interactive and contacts the
licensing host, so it cannot run unattended as a Job — run it from a machine
that can reach the database.

---

## 3. Resource requirements

Measured, not guessed.

| Component | Requests | Limits | Note |
|---|---|---|---|
| case-service | 200m / 256Mi | 750m / 768Mi | chart default; ~350–460Mi observed under load |
| engine, worker, extracted services | 200m / 256Mi | 750m / 768Mi | chart defaults |
| **PostgreSQL** | **500m / 1Gi** | **2 CPU / 3Gi** | **set by the setup script — the subchart default of 192Mi is OOM-killed under load** |
| Valkey | subchart default | | |

A full install with doubled replicas — 20 workload pods plus data stores — sat
at roughly **2.8 CPU and 8.6 GB** on the node under sustained load. 8 CPU /
16 GB is comfortable; 4 CPU / 8 GB runs the default replica counts.

### Connection ceiling — read this before scaling

Each service pod opens up to **21 PostgreSQL connections**: 10 operations + 5
overflow, 3 auth, 3 analytics (`services/_shared/velaris_shared/config.py`).

The bundled PostgreSQL ships `max_connections = 100`, which is **about four
pods**. The chart's own HPAs allow case-service to reach **10** and engine
**8** — so the autoscaler, left alone, exhausts its own database. Observed
directly: `TooManyConnectionsError` in the services and `psql` itself locked
out of the server, unable to connect to diagnose it.

The setup script therefore sets `max_connections = 500`. Budget roughly
**21 × (peak pod count) + 20** and raise it further before raising HPA ceilings,
or put PgBouncer in front.

---

## 4. Defects found while doing this

Twelve, each reproduced from a clean cluster. The first five block a
Kubernetes install outright; all of them existed in the repository when this
work started. Fixes applied here are marked *Fixed*.

1. **`engine/Dockerfile` cannot build.** The 13b extraction added `services/*`
   to the uv workspace, so `uv.lock` references `services/_shared`, which the
   engine Dockerfile never copies — `uv sync --frozen` fails. Broken since
   2026-08-23. **Still broken on 2026-08-26** — the earlier pass added the
   `DB_EXTRAS` arg to this file and left the cause alone. *Fixed 2026-08-26:*
   the Dockerfile now copies `services/_shared`, the member `uv.lock` names.
2. **`pyotp` missing from the case-service image.** Declared only in the root
   `pyproject.toml`, not in `services/case-service/pyproject.toml`. The host
   venv installs the root project so it works there; the image installs the
   service package and crashes on import. That image has never started.
   *Fix:* declare `pyotp` in the service's own dependencies.
3. **`prometheus_client` missing from all three extracted-service images.**
   `velaris_shared/observability/metrics.py` imports it and neither
   `_shared` nor the extracted services declare it. case-service only works by
   accident, because it declares it itself. *Fix:* declare it in `_shared`.
4. **Two migrations fail on every fresh PostgreSQL.**

   **051** — index names are unique per *schema*, not per table. `049` creates
   `ix_pd_status` on `process_definitions`; `051` creates the same name on
   `payment_disbursements` and fails. Under the host installer's
   error-swallowing psql simply moves to the next statement, so existing
   installs never got that index and `payment_disbursements(status)` is
   unindexed on them to this day. *Fixed:* renamed to `ix_pdisb_*`, with
   `IF NOT EXISTS`.

   **080** — `076` seeds two
   `scheduled_releases` rows with `enabled = FALSE`; `080` then runs
   `ALTER COLUMN enabled TYPE VARCHAR USING (CASE WHEN enabled THEN … ELSE NULL END)`
   while the column is still `NOT NULL` — the `DROP NOT NULL` is the next line,
   one statement too late. The column stays `boolean NOT NULL`. Existing installs
   escaped it only because their rows were already `TRUE`. *Fix:* drop the
   NOT NULL before the type change. *Fixed:* the drops now precede it.

   Both are already recorded as applied on existing installs, so these fixes
   change nothing for anyone running today — they repair the fresh-install path
   only. An install that ran the broken `051` is still missing
   `payment_disbursements(status)`; restoring it needs a new numbered
   migration, which has not been written.
5. **`setup-velaris.sh` swallows migration failures.** It pipes migrations
   through `psql` with no `ON_ERROR_STOP`, so the failure above exits 0, is
   recorded in `schema_migrations`, and setup prints ✓. Proven: exit code 0,
   error on stderr, column unchanged before and after. *Fix:* add
   `-v ON_ERROR_STOP=1` and stop on failure.
6. **The chart runs no migrations and no bootstrap.** A successful
   `helm install` yields a database with **0 tables** and a platform that
   answers 503 to everything. Handled here by the setup script; a Job in the
   chart would be better.
7. **Worker `imagePullPolicy` is missing entirely** from
   `worker-deployment.yaml`, so `:latest` defaults to `Always` and the worker
   can never start on an air-gapped or preloaded-image cluster. Every other
   service sets it. Proven on 2026-08-26: the rendered worker carries no
   policy, and a pod created from an unset policy on a `:latest` tag comes back
   `imagePullPolicy: Always`. It bites only on the chart's DEFAULT tag, which is
   why runs with an explicit `VELARIS_IMAGE_TAG` never saw it.
   *Fixed 2026-08-26:* `image.worker.pullPolicy`, defaulting to `IfNotPresent`
   like every sibling.
8. **Chart pins are dead.** `valkey 4.0.2` no longer exists in the Bitnami repo
   (oldest is 6.x) so `helm dependency update` fails outright, and
   `postgresql 15.5.14` is PostgreSQL 16.3, whose image has also left the
   `bitnami/` namespace. **This was recorded here but never applied**, so every
   track still died at "Step 3/7 — Chart dependencies" on 2026-08-26.
   *Fixed 2026-08-26:* `Chart.yaml` now pins `postgresql 18.8.13` (PG 18.6)
   and `valkey 6.2.12` (Valkey 9.1.1).

   Note what the pins now buy you and what they do not: Bitnami's 2025 policy
   left only `:latest` free, so both subcharts run **`bitnami/*:latest`** — the
   chart version is pinned, the image is not. A node that pulls fresh months
   from now can get a new major PostgreSQL on an old `PGDATA`, which refuses to
   start. Pin it yourself before that matters:
   `--set postgresql.image.tag=<digest-or-tag>`, or use an external database.
9. **The bundled PostgreSQL is OOM-killed under load** at the subchart's default
   192Mi — see §3. It cost **13× throughput**: 79 rps before, 1,050 rps after.
10. **`helm upgrade` conflicts with the HPA.** The chart writes `.spec.replicas`
    on deployments that also have an HPA, so once anything has scaled, upgrade
    fails with a field-manager conflict. The update script works around it by
    pinning live counts; the real fix is to omit `replicas` when
    `autoscaling.enabled`.

11. **`/ready` answered 200 while reporting the database disconnected.** The
    handler runs a real `SELECT 1`, sets `"status": "degraded"` and
    `"database": "disconnected"` in the body — and returns **200** either way. A
    readiness probe is decided by the status code, so a pod that could not
    reach its database stayed **Ready** and kept taking traffic. Worse,
    case-service was the one deployment probing **`/health` for readiness**
    (the extracted services already split `/ready` and `/health` correctly), so
    the deep check was not even wired in. Proven by running case-service
    against MySQL on an image built without the MySQL driver: every request
    failed with `ModuleNotFoundError: No module named 'aiomysql'` while both
    pods sat `1/1 Running`, and a rolling update rolled forward over them.
    *Fixed 2026-08-26:* `/ready` returns **503** when the database check fails,
    and the chart's case-service readinessProbe points at `/ready`. Re-proven:
    the same driverless image now stays `0/1`, the rollout stops, and the
    healthy pods keep serving.
12. **The ingress rendered a backend named `<release>-helix-`** when a path
    entry omitted `service:`. The key is chart-specific, so an operator writing
    the ordinary Helm ingress values gets an ingress that resolves to nothing —
    valid YAML, accepted by the API server, 503 at the controller.
    *Fixed 2026-08-26:* `service` defaults to `case-service` and `port` to 8200.

13. **The ENGINE still had the 2026-08-26 shape.** Its readiness probed
    `/health` — which only says the process is alive — and it has an in-memory
    fallback: with the database unreachable it keeps deployed processes in one
    pod's memory, loses them on restart, and every replica holds a different
    set. Such a pod reported **Ready** and took traffic. Its `/ready` answered
    **200** with `"database": "unavailable"` in the body, which no probe reads,
    and it had no liveness probe at all. *Fixed 2026-09-17:* `/ready` returns
    **503** without a database, the chart's engine readinessProbe points at
    `/ready`, and a liveness probe on `/health` was added. Every HTTP deployment
    also gained a **startupProbe** (5s x 30 = 150s), so the boot budget is stated
    once rather than guessed in a liveness delay. Pinned by
    `tests/phase32/test_scaling.py::test_10b_every_http_deployment_probes_correctly`.

14. **The case-service IMAGE could not start.** 25 modules import
    `hxarbiter_core`, one of them (`agents/attribution.py`, reached from
    `api/routers/hxnexus.py`) at import time — and `libs/hxarbiter-core` is a
    workspace library that the Dockerfile never copied in. Every case-service
    image built since HxArbiter shipped exited with
    `ModuleNotFoundError: No module named 'hxarbiter_core'` before serving a
    request. The host install hid it completely: there the library is an
    editable install in the workspace virtualenv. *Fixed 2026-09-17:* the
    Dockerfile copies and installs it; pinned by
    `test_10e_the_case_service_image_carries_hxarbiter_core`.
15. **Our workloads had no securityContext, and the database password was in
    plain sight.** The bundled PostgreSQL and Valkey subcharts ship
    `runAsNonRoot`, dropped capabilities and a read-only root; our six
    deployments had none of it, mounted the default ServiceAccount token into
    pods that never call the Kubernetes API, and carried the database URL —
    password included — as an inline env value, readable by anyone who can
    `get deployments`. *Fixed 2026-09-17:* pod and container security contexts
    from one helper, `automountServiceAccountToken: false`, and the URL in a
    Secret (`databaseSecret.existingSecret` to bring your own).
16. **`runAsNonRoot` needs a NUMERIC uid.** With the images declaring
    `USER helix` / `USER velaris`, every pod failed with *"image has non-numeric
    user (helix), cannot verify user is non-root"* — `CreateContainerConfigError`,
    before the container started. The engine image ran as **root** outright.
    *Fixed 2026-09-17:* every image pins uid/gid 10001 and the engine gained a
    user. Verified in-cluster: `id` → `uid=10001`, `CapEff: 0000000000000000`,
    `NoNewPrivs: 1`, no ServiceAccount token on disk.
17. **Uploaded documents had nowhere to go.** The default store is
    `./data/velaris-docs` — `/app/data` at the image's WORKDIR — which is
    root-owned, so an upload by the non-root user failed as a 500; and even
    writable, a container path means documents vanish on restart and each
    replica holds a different set. *Fixed 2026-09-17:* the image creates
    `/app/data` owned by the user, the chart gained
    `caseService.documents.persistence` (a PVC, mounted at `/app/data`) and
    `podSecurity.fsGroup: 10001` so a mounted volume is writable — most storage
    classes present one as `root:root 0755`, and kind's local-path provisioner
    hands out `0777`, which hides the problem on a test cluster.
18. **Nothing stopped pod-to-pod traffic.** Any pod in the cluster could reach
    case-service on 8200 directly, bypassing the gateway that exists to be the
    only door. *Added 2026-09-17:* an opt-in `networkPolicy` (off by default —
    a policy is worthless where the CNI ignores it and disruptive where it does
    not) plus a `NOTES.txt` that says so after every install, along with the
    worker's Temporal dependency and the document-storage choice.

### Verified on a live cluster, 2026-09-17

kind v0.24 / Kubernetes v1.31, chart installed from scratch with the bundled
PostgreSQL and Valkey:

* all five HTTP services **1/1 Ready**; `helm upgrade` reports `deployed`;
* `kubectl apply --dry-run=server` accepts every rendered object;
* in-cluster `GET /health` **200**, `GET /ready` **200** (`database: connected`),
  `GET /api/v1/cases` **401** — the API is up and still refuses an anonymous call;
* **the probe split proven by taking the database away**: `kubectl scale
  statefulset postgresql --replicas=0` → case-service and engine went
  **0/1 NotReady** within ~2 minutes and their Service endpoints emptied (no
  traffic routed), **restart count stayed 0** (liveness did not kill them), and
  both returned to 1/1 by themselves when PostgreSQL came back;
* the worker CrashLoopBackOffs without a Temporal to reach — the chart does not
  install one, which `NOTES.txt` now says at install time.

Fixed during this work: `DELETE /api/v1/case-types/{id}?hard=true` raised an
unhandled `IntegrityError` (**500**) when cases still referenced the type. It
now returns **409** with the count (`case_types.py`). This affected the host
install identically.

---

## 5. Security

### What the chart does not give you

- **No `securityContext` on any of the six Velaris workloads** — no
  `runAsNonRoot`, no `readOnlyRootFilesystem`, no
  `allowPrivilegeEscalation: false`. The only ones in the rendered output come
  from the Bitnami subcharts.
- **The engine container runs as root** (uid 0); its Dockerfile has no `USER`.
  case-service and the extracted services correctly run as uid 999.
- **No NetworkPolicy for any Velaris pod.** Anything in the cluster can reach
  any service directly. The Bitnami subcharts bring their own; the platform has
  none.
- **No ServiceAccount and no `automountServiceAccountToken: false`** — every pod
  runs as `default` with a mounted API token it never uses.
- **The database password is a plaintext env var** in every deployment
  (`VELARIS_CASE_DATABASE_URL`), not a Secret reference.
- **The ingress template has no TLS block at all.** Exposing it is plain HTTP.
- **No PodDisruptionBudget** for any Velaris workload.

### No gateway

On the host, nginx on 8200 is the only door and workload identity refuses
direct calls. **The chart has no gateway**: services are exposed directly and
`VELARIS_GATEWAY_IDENTITY_SECRET` is unset, which each service reports at
startup — *"Workload identity is OFF … any local process can reach this service
directly instead of through the gateway."* Combined with the absent
NetworkPolicy, every service is directly reachable by anything in the cluster.

### Rate limiting is bypassable

The chart sets `VELARIS_CASE_RATE_LIMIT_BACKEND: "redis"` (on the host it
defaults to `memory`), so Valkey is load-bearing here. The limiter is 120 rpm
with a burst of 30, correctly shared across replicas through Redis.

It keys on `X-Forwarded-For` and **trusts it unconditionally**. Measured from
one client, one source IP: **30 of 80** requests allowed with no header,
**80 of 80** with a rotating `X-Forwarded-For`. Behind a gateway that overwrites
the header this is contained; in this chart nothing overwrites it and nothing
stops a pod from setting it. Fix by trusting the header only from known proxy
addresses.

### What holds up well

- An anonymous sweep of the live route table — **227 of 235** parameterless GET
  routes require authentication. The six that answer anonymously are `/health`,
  `/health/deep`, `/ready`, `/api/v1/status/public`,
  `/api/v1/releases/flags` and `/api/v1/auth/real/sso/providers`; each was
  inspected and none leaks credentials, hostnames or versions.
- Authentication rejects all seven attack shapes tested (absent, garbage,
  tampered signature, empty, wrong scheme, wrong password, unknown user).
- Body limit enforced at 10 MB with `VEL-SYS-BODY-413`.
- Injection and XSS strings are handled as data, not executed.

### Before exposing this to anyone

1. Put an ingress with TLS in front — the chart gives you neither.
2. Restrict `X-Forwarded-For` to trusted proxies, or the rate limit is decorative.
3. Add NetworkPolicies so only the gateway reaches the services.
4. Move the database password to a Secret reference.
5. Set `securityContext` on all six workloads and give the engine a non-root user.
6. Set `VELARIS_GATEWAY_IDENTITY_SECRET`, or accept that workload identity is off.

---

## 6. Behaviour under load and failure

Load generated from inside the cluster against `GET /api/v1/cases`, 4→10
case-service replicas, PostgreSQL at 2 CPU / 3 GB.

| Concurrency | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| 40 | 1,050 rps | 27 ms | 83 ms | 112 ms | 9 / 63,029 (connection ceiling) |
| 100 | 1,173 rps | 83 ms | 115 ms | 343 ms | **0 / 105,553** |
| 200 + chaos | 795 rps | 181 ms | 504 ms | 3,011 ms | **0 / 95,384** |

The chaos run killed two case-service pods and Valkey mid-test.

| Failure | Result |
|---|---|
| Kill 1 of 2 case-service pods | 57/57 requests OK; replacement ready in 16 s |
| Analytics + hxgraph to zero, workers killed | case-service unaffected |
| case-service to zero | every other service still 200 |
| `helm upgrade` under live traffic | 60/60 requests OK |
| Kill Valkey (2 replicas, 3 s client timeout) | 4 requests lost, self-healed |
| Kill Valkey (10 replicas, 15 s timeout) | no errors, visible as latency |
| **Kill PostgreSQL** | **~78 s of failures, 18 requests lost**, then full self-heal with no restart |

Services isolate properly and recover on their own. **PostgreSQL is the single
point of failure** — one replica, no HA, and the chart offers no read replica or
failover. Plan for it before production.

---

## 7. Functional coverage

`K8/Postgres/` ships no test harness; the suite used here lives in the session
scratchpad. What it verified, 100% green across eight consecutive runs
(55 checks), including immediately after a live `helm upgrade`:

- **Happy path** — health, ready, metrics, `auth/me`, cases (plain and
  paginated), case-types, user-directory, analytics overview, graph report; and
  the full write cycle: create case-type → create case → read → PATCH → soft
  delete → confirm it leaves listings → hard delete → 404.
- **Sad path** — 8 authentication rejections; 20 validation cases (unknown
  route, missing id, malformed UUID, wrong method, malformed JSON, missing and
  null fields, wrong content type, pagination bounds, injection, XSS, unicode,
  path traversal); and the limits (10 MB → 413, burst → 429).

Two behaviours worth knowing when writing tests against this API:
`DELETE /api/v1/case-types/{id}` is a **soft delete** — 204, still readable by
id, absent from listings — and `PATCH /api/v1/cases/{id}` takes
`CaseDataUpdate`, so `data` is required and `priority` is not patchable there.
`HEAD` on a GET route returns 405, which is ordinary FastAPI behaviour but will
read as "down" to a HEAD-based monitor.

---

## 8. Cross-engine results and the commit fix

MySQL and MariaDB were added after this document was first written. Both live in
sibling directories (`K8/MySQL/`, `K8/MariaDB/`) and the chart now carries
optional `mysql` and `mariadb` subcharts — **both disabled by default, so a
PostgreSQL install renders exactly as before**.

| Engine | Version | Functional suite | 40 conc | 100 conc |
|---|---|---|---|---|
| PostgreSQL | 18.6 | 55/55 ×3 | 411 rps, 0 errors | 363 rps, 0 errors |
| MySQL | 9.4.0 | 55/55 ×3 | 374 rps, 0 errors | 341 rps, 0 errors |
| MariaDB | 13.0.1 | 55/55 ×3 | 379 rps, 0 errors | 343 rps, 0 errors |

All three at two case-service replicas with no autoscaler, so these are
comparable to each other but not to §6, which ran 4–10 replicas under an HPA.
PostgreSQL leads by roughly 6–10%.

### The commit-timing defect — fixed platform-wide

Adding MySQL exposed a defect that had always been present here too.
`get_session` is a FastAPI `yield` dependency whose commit runs in the request's
exit stack, which FastAPI unwinds **after the response has been sent** — so the
API returned `201 {"id": …}` before the row was committed. On MySQL, 2 of 8
immediate read-after-write pairs 404'd (10 of 12 under load); a hard-deleted row
read back as still present. PostgreSQL's window is too small to observe, but the
race was identical.

`velaris_shared/db/commit_route.py` now commits every session registered for a
request between the handler returning and the response being written, and a
failed commit surfaces as a 500 instead of a success reply over data that was
never written. **PostgreSQL was re-installed from scratch and re-verified after
this change: 162 migrations, 55/55 three times, no regression.**

Two traps recorded in `K8/MySQL/README.md` §7: a route class reaches none of the
routes (`include_router` overrides it), and annotating the dependency
`Request | None` stops FastAPI from starting at all.

---


### Re-verified at 8 CPU / 16 GiB — all three engines, like for like

Same node cap, same chart, two case-service replicas each and no autoscaler, so
the engines are directly comparable. Zero errors across 154,363 requests.

| Engine | Functional suite | 40 conc | p95 | 100 conc | p95 |
|---|---|---|---|---|---|
| PostgreSQL 18.6 | 56/56 ×3 | 387 rps | 176 ms | 362 rps | 686 ms |
| MySQL 9.4.0 | 56/56 ×3 | 350 rps | 186 ms | 305 rps | 712 ms |
| MariaDB 13.0.1 | 56/56 ×3 | 354 rps | 159 ms | 322 rps | 675 ms |

PostgreSQL leads by 10–19%; MySQL and MariaDB are within a few percent of each
other. These are lower than the HPA figures elsewhere in this document because
those ran 4–10 replicas — this set fixes replicas so the database is the only
variable.

---


### Multi-node, and a real ingress with TLS

Both were listed as unproven in earlier revisions of this document. Both are now
done.

**Three-node cluster** (1 control-plane + 2 workers, each capped 3 CPU / 5 GB).
162 migrations applied, pods scheduled across both workers (6 + 6), and the
functional suite **56/56 three times** — so cross-node service-to-service
traffic, the shared database and the migration runner all behave as they do on
one node.

Under load with pods killed on one worker, though, multi-node differs:

| | Requests | Failures |
|---|---|---|
| single node, pod kill under load | 30,391 | **0** |
| three nodes, 2 of 3 case-service pods killed on one worker | 26,561 | **29 (0.11%)** |

All 29 were transport-level (`ConnectError`, `ReadError`,
`RemoteProtocolError`) — connections already in flight to the deleted pods.
The chart defines **no `preStop` hook and no PodDisruptionBudget**, so a pod
stops accepting connections before its endpoint has been withdrawn everywhere.
On one node the replacement is local and the window is invisible; across nodes
it is not. A `preStop: sleep 5` and a PDB would close it — neither is in the
chart today.

**Ingress with TLS.** The template previously had no TLS block at all
(§4). It now supports `ingress.tls` and `ingress.annotations`, both empty by
default so existing installs are untouched. Verified against a real
ingress-nginx controller with a self-signed certificate for `velaris.test`:

- `http://` → **308 redirect** to https
- TLS 1.3 handshake, certificate served
- anonymous `/api/v1/cases` → **401**
- login over TLS → token issued; authenticated request → **200**
- `/api/v1/analytics` routed to its own service through the same ingress

A note for whoever runs this next: the multi-node cluster would not start until
`fs.inotify.max_user_instances` was raised from 128 to 512 — kind nodes fail
with `Failed to allocate manager object`, which looks like a Velaris problem and
is not one.

---


### Closing the last five gaps

Everything previously listed as unproven has now been done, and two of the five
turned into real fixes rather than confirmations.

| # | Item | Outcome |
|---|---|---|
| 1 | `setup-velaris.sh` swallowed migration failures | **Fixed.** `-v ON_ERROR_STOP=1` on the PostgreSQL loop plus a clear failure message on both loops. Proven: the same failing file exits **0** before and **3** after, so the installer stops instead of recording it as applied. |
| 2 | In-flight requests lost when a pod is deleted | **Fixed.** `preStop` drain (`terminationDrainSeconds`, default 5s) on all six workloads, `terminationGracePeriodSeconds` raised to match, and an opt-in PodDisruptionBudget. Killing 2 of 3 case-service pods on one worker: **29 failures in 26,561 → 1 in 28,455** (0.11% → 0.004%). |
| 3 | Engine only proven to start on MySQL/MariaDB | **Exercised, and it found a defect.** `/ready` reports `database: connected` and `/processes` returns a real query result. But the engine reads plain `TEMPORAL_HOST` (default `localhost:7233`) while the chart only set Temporal for the *worker* — so the engine reported `temporal: unavailable` forever with a healthy Temporal next door. **Fixed**; it now reports `status: ready, database: connected, temporal: connected`. |
| 4 | Only newest engine versions tested | **MySQL 8.4.5** and **MariaDB 11.8.2** — the versions the host track targets — both installed, 24 migrations, utf8mb4, **56/56 twice each**. |
| 5 | Restore-from-dump, cloud managed database | **Dump/restore proven**: `pg_dump` of a live install (258 tables) restored into a new database, installed against it, **56/56**. A cloud managed database (RDS/Cloud SQL) is **still untested** — it needs credentials and network access this environment does not have. |

Two traps worth carrying forward:

- Older Bitnami charts refuse a `bitnamilegacy/` image with an `insecureImages`
  error; they need `global.security.allowInsecureImages=true`. The newer charts
  do not.
- A migration failing with **exit 137** is the node out of memory, not a broken
  migration. Six namespaces on one 16 GB node was enough to OOM-kill MySQL
  mid-baseline and briefly take the API server down with it.

---

## 9. How these scripts were verified

Every script here was run against a live cluster; none of this is a desk check.

### The install path, end to end

`setup-velaris-k8s.sh` was pointed at an empty namespace (`velaris-fresh`) on a
cluster where nothing of Velaris existed:

- chart installed, subchart dependencies resolved;
- **all 162 migrations applied, zero failures** — the run that proved the `051`
  and `080` fixes, since both had stopped the same runner minutes earlier;
- 12/12 pods ready; **256 tables and 162 rows in `schema_migrations`**;
- after bootstrapping a superadmin, the functional suite scored
  **55/55, 100% green** against that install.

So a namespace created from nothing by this script produces a platform that
passes the same suite as the hand-built one.

### The other paths

| Path | Result |
|---|---|
| `setup` against an existing release | refuses, points at `update-velaris-k8s.sh` |
| `setup --dry-run` | renders 30 objects, changes nothing |
| `update --dry-run` | lists pending migrations, applies none |
| `update` on a database with an unexplained schema | refuses, prints the `VELARIS_K8S_ADOPT=1` remedy |
| `update` with adoption | ledger backfilled with 162 files, 0 applied |
| `update` (real) | revision 7 `Upgrade complete`; suite still 55/55 afterwards |
| `stop` | 6 deployments to zero, data kept, scale recorded |
| `stop --start` | all workloads restored |

### Defects found in these scripts, and fixed

Running them is what found them:

1. **`update --dry-run` applied the migrations.** The migration step ran before
   the dry-run check. A dry run must not write to the database. It now lists
   what *would* apply and stops.
2. **The rollback targeted a revision that was still deployed.** A failed
   `helm upgrade` often leaves the previous revision live, and asking helm to
   roll back to the revision you are already on is an error, not a recovery.
   It now resolves the last revision helm actually has `deployed`, and says
   "nothing to roll back" when that is the truth.
3. **`wait_ready` counted every pod in the namespace.** A leftover pod from an
   earlier failed revision — or an unrelated pod sharing the namespace — never
   goes ready, so a perfectly healthy upgrade looked failed and was **rolled
   back**. This bit repeatedly during the MySQL work, silently undoing good
   upgrades. It now tracks only the release's own deployments via
   `kubectl rollout status`.
4. **The stop script patched the HPAs pointlessly.** It set `minReplicas: 1` —
   already the value — creating a `kubectl-patch` field manager that then broke
   the *next* `helm upgrade` with a conflict on `.spec.minReplicas`. Removed: a
   HorizontalPodAutoscaler does not act on a deployment sitting at zero, so
   scaling to zero is a complete stop on its own.

5. **`VELARIS_IMAGE_REGISTRY` and `VELARIS_IMAGE_TAG` never reached helm.**
   Both are documented at the top of every script as the way to point an
   install at your own registry or a pinned tag, and both were used only to
   build and load. The chart kept deploying its own `helix/*:latest`, so an
   operator who set either variable got a full install in **ImagePullBackOff**
   — the images they had just built sitting unused on the node. `_common.sh`
   now builds the `--set image.*` list and setup passes it. The update script
   passes it **only when one of the variables is actually set**, so
   `--reuse-values` keeps meaning what it says.
6. **The external-database client pod was never removed.** `remove_client_pod`
   existed and nothing called it, so every run against
   `VELARIS_K8S_EXTERNAL_URL` left a `sleep 7200` pod in the operator's
   namespace. Now removed by an `EXIT` trap, which fires on `die()` too.
7. **`update` printed "upgraded to revision " with nothing after it.** It read
   a top-level `revision` key out of `helm status -o json`, which helm 4 does
   not emit. It now reads the revision the same way the rollback target is
   read.
8. **A field-manager conflict printed helm's raw apply error and stopped.**
   Anything done by hand — `kubectl set image`, `edit`, `scale` — takes
   ownership of that field, and helm then refuses to apply over it. The refusal
   is correct and nothing is changed, but the operator was left with a wall of
   `conflict with "kubectl-set"` and no next step. The script now says what
   happened and prints the one command that hands ownership back. The
   `--field-manager=helm` in it is not optional: applying under any other name
   moves the ownership sideways and the next upgrade conflicts again — that
   was measured, not assumed.

A ninth was self-inflicted during testing and is worth recording as a warning:
editing a shell script **while it is running** makes bash re-read the file at
shifted offsets and fail with a syntax error mid-run. The install that appeared
to break at "Waiting for the database" was that, not the script.

### Verified against

kind v0.32.0, Kubernetes v1.36.1, Helm v4.2.4, kubectl v1.36.4, Bitnami
postgresql 18.8.13 (PostgreSQL 18.6) and valkey 6.2.12 (Valkey 9.1.1),
Temporal 1.25 on its own PostgreSQL 16.

**Not verified:** nothing from the original list remains — external databases,
multi-node and ingress TLS are all covered above, and MySQL/MariaDB now have
their own directories. Still open: a cloud provider's managed database, and
restoring from a dump taken by `backup-velaris.sh` rather than a live database.

---

## 10. Re-verified 2026-08-26

The whole thing was taken apart again from a cold start: no cluster, no
`kind`/`kubectl`/`helm` on the machine, nothing but the repository. That is the
only way to find what the previous run had been carrying in its hands rather
than in the files.

**Three things stopped a clean install outright**, and all three were already
*written down* in §4 as known — written down, and never applied:

1. `helm dependency update` failed on `valkey 4.0.2`. Every track died at
   Step 3/7. Nobody can install this chart from the repository as it stood.
2. `engine/Dockerfile` still could not build.
3. Setting `VELARIS_IMAGE_TAG` produced a full install in ImagePullBackOff.

The lesson is the one worth keeping: **the earlier runs were driven from a
patched copy of the chart in a scratch directory**, so the scripts had never
once been proven against `deploy/helm/velaris` as committed. A verification
that does not start from the repository proves the operator's copy of nothing.

### What was run, and what it found

| Path | Result |
|---|---|
| PostgreSQL 18.6, bundled subchart, clean install | **58/58 green** |
| MySQL 9.4.0, bundled subchart, clean install | **58/58 green** |
| MariaDB (Bitnami chart 27.0.7), clean install | **58/58 green** |
| PostgreSQL, install pointed at an existing external database | **58/58 green** |
| `uninstall` (PVCs kept) then re-install onto the same volumes | data intact, 0 migrations applied, **58/58 green** |
| TLS ingress, real ingress-nginx, self-signed cert | 200 over https, http → 308, unknown host → 404, all four backends routed |
| `stop` / `--start` | six deployments to zero and back, state file cleaned up |
| hand-scaled deployment, then `update` | 3 replicas preserved, no HPA conflict |
| `update` after a hand-run `kubectl set image` | refused, nothing changed, recovery printed — and the printed recovery works in one shot |
| foreign (non-Velaris) database | refused on all three engines |
| populated schema with an empty ledger | refused; `VELARIS_K8S_ADOPT=1` backfills without executing |
| a migration that fails | stops, is **not** recorded, says the schema is part-migrated |
| no superadmin | health/ready/metrics 200, every API route 503 |
| 4-byte UTF-8 (emoji, CJK ext-B, ZWJ family) on MySQL and MariaDB | stored and read back byte-identical; emoji search 200 |

The suite is 58 checks now rather than 55: the extra ones are the engine's own
health endpoint and, more usefully, **eight immediate read-after-write pairs
plus a hard-delete read-back** — the shape that exposed the commit-timing
defect in §8. They pass on all three engines.

### The engine was running on nothing at all

Two defects, found by reading what the engine actually says at boot rather than
watching it go green. Both are **platform-wide, not Kubernetes-only**.

- **The engine never reached the database.** It reads plain `DATABASE_URL`
  (`helix_engine/db/session.py`); the chart set `VELARIS_ENGINE_DATABASE_URL`,
  which **nothing in the repository reads**. So it fell back to its compiled-in
  default — `localhost:5432` — got `Connection refused`, and **silently
  continued on an in-memory store**, logging
  `VELARIS_engine_ready database=in-memory` and passing its health probe. Every
  engine pod ran with no persistence and no shared state, and nothing said so.
  *Fixed:* the deployment now sets `DATABASE_URL` (and
  `VELARIS_CASE_DATABASE_BACKEND`, which every other workload already had).
  `VELARIS_ENGINE_DATABASE_URL` is kept: an install may hold it, and removing a
  variable an install holds is not something an update gets to do.
  Now: `database=postgresql`.

- **`velaris_sdk.protocols` could not be imported — by anyone.** Ten of its
  seventeen re-exports name a class the module does not define
  (`EventBusBackend` vs `Event_busBackend`, `AuditStorage` vs `AuditBackend`,
  and eight more), so `from velaris_sdk.protocols import …` raised `ImportError`
  at the first mismatch. The engine catches that around its Temporal setup and
  carries on: `temporal=unavailable`, forever, on a cluster where a perfectly
  healthy Temporal was answering the worker next door. *Fixed:* each module now
  also binds the name the package exports; both names work, so nothing that
  imports the existing one changes. The engine now logs `temporal_connected`
  and starts its own worker — `task_queue=helix-engine`, 7 activities and
  `ProcessWorkflow` registered. As far as these runs can tell, that had never
  happened before.

  **Worth checking on a host install too.** `DATABASE_URL` is not in `.env` and
  not in `velaris-engine.service`, so the host engine is also running on its
  compiled-in default — which resolves to localhost there and therefore
  *appears* to work, provided the local database really is `helix:helix_dev_
  password@localhost:5432/helix`. If it is not, that install's engine is on an
  in-memory store as well. `journalctl -u velaris-engine | grep -E
  "database_(connecting|unavailable)"` answers it in one line.

### Two engine differences worth knowing before you touch data by hand

- **The MySQL/MariaDB baseline carries no column defaults.** PostgreSQL has
  `id UUID PRIMARY KEY DEFAULT gen_random_uuid()` and `failed_attempts INT
  DEFAULT 0`; the MySQL baseline, generated from the SQLAlchemy models, has
  `id CHAR(36) NOT NULL` and `failed_attempts INT NOT NULL` with no defaults at
  all. The application never notices — SQLAlchemy supplies both in Python — but
  a hand-written `INSERT` that works on PostgreSQL fails on MySQL with
  `Field 'failed_attempts' doesn't have a default value`. Bootstrapping a
  superadmin with raw SQL is exactly that case: name every column.
  `INSERT IGNORE` makes it worse, not better — it downgrades the error to a
  warning and writes an empty string as the primary key, which then surfaces
  far away as `ValueError: badly formed hexadecimal UUID string` on login.
- **A failed migration leaves the statements before it committed.** Both `psql`
  and `mysql` autocommit statement by statement; `ON_ERROR_STOP` stops the run,
  it does not undo it. Per-file transactions are not available here: 21
  PostgreSQL migrations open their own `BEGIN`, and MySQL DDL is not
  transactional at all. The runner reports it honestly ("the database is
  part-migrated") rather than pretending otherwise.

### Verified against, 2026-08-26

kind v0.32.0, Kubernetes v1.36.1, Helm v4.2.4, kubectl v1.37.0, one node capped
at **8 CPU / 16 GB**, Temporal 1.25 on its own PostgreSQL 16, ingress-nginx
with a self-signed certificate. Engines: PostgreSQL 18.6 (Bitnami chart
18.8.13), MySQL 9.4.0 (chart 14.0.3, `bitnamilegacy/mysql`), MariaDB (chart
27.0.7, `bitnami/mariadb:latest`).

**Still not covered:** a cloud provider's managed database (needs credentials
this machine does not have), and multi-node (`fs.inotify.max_user_instances` is
128 here; kind needs 512, which is a root change). Restore-from-dump and
multi-node were both proven on 2026-08-25 — see §8 — and were not re-run.
