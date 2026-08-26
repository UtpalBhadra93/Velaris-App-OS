# Velaris on Kubernetes — MySQL

Running Velaris on Kubernetes against **MySQL**, using the chart at
`deploy/helm/velaris` plus the three scripts in this directory. The MariaDB
track is the sibling directory `K8/MariaDB/`; PostgreSQL is `K8/Postgres/`.

Everything below was established by installing on a real cluster and breaking it
on purpose. The numbers are measured.

**Verified on** kind v0.32.0 / Kubernetes v1.36.1, Helm v4.2.4, Bitnami mysql
chart 14.0.3 (**MySQL 9.4.0**), valkey 6.2.12, node capped at 8 CPU / 16 GB.

**Result: 58/58 functional checks green** on MySQL 9.4.0 (Bitnami chart
14.0.3), re-verified from a cold start on **2026-08-26** at 8 CPU / 16 GB — see
§9. The 55/55 figures below are the 2026-08-25 runs, kept because the load and
failure numbers were measured then.

---

## 1. Scripts

```bash
./K8/MySQL/setup-velaris-k8s.sh --build --kind my-cluster   # build, load, install
./K8/MySQL/setup-velaris-k8s.sh --dry-run                   # render only
./K8/MySQL/update-velaris-k8s.sh --dry-run                  # list pending migrations
./K8/MySQL/update-velaris-k8s.sh                            # migrate, upgrade, verify, roll back on failure
./K8/MySQL/stop-velaris-k8s.sh                              # scale to zero, keep data
./K8/MySQL/stop-velaris-k8s.sh --start                      # resume
./K8/MySQL/stop-velaris-k8s.sh --uninstall                  # remove release, keep PVCs
./K8/MySQL/stop-velaris-k8s.sh --purge                      # DELETES THE DATABASE
```

Default namespace is `velaris-mysql` (`VELARIS_K8S_NAMESPACE` overrides), release
`velaris`. Other variables: `VELARIS_CHART_DIR`, `VELARIS_IMAGE_REGISTRY`,
`VELARIS_IMAGE_TAG`, `VELARIS_K8S_ADOPT`.

The scripts are the PostgreSQL set with three engine values changed
(`DB_ENGINE`, `DB_CLIENT`, `DB_SECRET_KEY` at the top of `_common.sh`) and the
MySQL-specific install defaults below. Everything else — the migration ledger,
the adoption guard, replica pinning, rollback — behaves identically; see
`K8/Postgres/README.md` §1 for that contract.

### What this track sets

| Setting | Why |
|---|---|
| `mysql.image.repository=bitnamilegacy/mysql` | Bitnami moved versioned free images to `bitnamilegacy/` in 2025; the tag this subchart pins no longer exists under `bitnami/`, and without the override the pod cannot pull at all |
| `mysql.primary.extraFlags=--max_connections=500 --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci` | Connection ceiling, and utf8mb4 **before** the database is created — see §4 |
| `mysql.primary.resources` 500m/1Gi → 2 CPU/3Gi | The subchart's default preset is far too small for real load |
| Images built with `--build-arg DB_EXTRAS="[mysql]"` | The MySQL driver is an optional extra; without it no service can connect |

**Pin an image tag per engine.** The chart defaults every image to `:latest`,
so a cluster hosting more than one engine shares one tag: re-pointing `latest`
at a PostgreSQL build silently gave the MySQL namespace an image with no
`aiomysql`, and every request became a 500 on the next restart. Build to
`:mysql` / `:pg` and set `image.*.tag` per install.

**Never use `mysql.primary.configuration`** to set `max_connections`. That key
replaces Bitnami's entire `my.cnf`, and dropping its `[client]` section sends
`mysqladmin` to the wrong socket — the server runs fine while its health probe
fails forever, so the pod never goes ready. `extraFlags` appends instead.

- **The engine has no MySQL driver at all.** `engine/pyproject.toml` declares
  only `asyncpg` and no `mysql` extra. The engine Dockerfile installs
  `aiomysql`/`pymysql` alongside the locked environment when `DB_EXTRAS` is set,
  because adding the extra to `pyproject.toml` would invalidate `uv.lock`, which
  `uv sync --frozen` rejects.
- **The migration track is different**: `migrations/mysql/` — a frozen baseline
  of PostgreSQL 001–154, three seeds, then one numbered file per PostgreSQL
  migration from 155. 24 files today, against PostgreSQL's 162.
- **MySQL 9.4** is what the current chart ships. The host track was verified on
  MySQL 8.4; nothing in this run depended on the difference.

---

### Converting an existing utf8mb3 install

`convert-utf8mb4.sh` repairs a database created before the setup script began
forcing utf8mb4. **Dry run by default** — it reports the affected tables and
names any index over 191 characters, which is what makes a conversion fail half
way at 4 bytes per character. `--apply` converts.

It rebuilds every affected table, so take a backup and run it out of traffic.
Verified on a synthetic utf8mb3 database: the dry run changed nothing, `--apply`
converted both tables, and a 4-byte emoji then stored and read back correctly
(`F09F92A5`). New installs never need it.

---


### Reusing an existing Velaris database

An earlier Velaris install being moved into Kubernetes, a restored dump, or a
database that lives outside the cluster. The bundled database is then not
installed at all:

```bash
VELARIS_K8S_EXTERNAL_URL="mysql+aiomysql://user:pass@host:3306/velaris" \
  ./K8/MySQL/setup-velaris-k8s.sh
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
optional and only costs you autoscaling. They are the same for all three
engines — only the database subchart differs.

| # | What | Why it is required |
|---|------|--------------------|
| 1 | **A reachable cluster with a default StorageClass** | The bundled database and Valkey each claim a PVC. With no default class they stay `Pending` and nothing ever starts. Any conformant Kubernetes: EKS, GKE, AKS, OpenShift, k3s, kind. |
| 2 | **`kubectl` and `helm` v3+** on the machine you run the script from — `docker` too, but only if you build images | `require_cluster` checks `kubectl cluster-info` and stops if nothing answers. |
| 3 | **A container registry the cluster can pull from**, named in `VELARIS_IMAGE_REGISTRY` | The cluster cannot see images built on your laptop. `kind load` is a kind-only shortcut; on a vendor cluster you build and push. |
| 4 | **Temporal, already running** | A hard dependency the chart does **not** deploy. The worker connects to `worker.temporalHost` and crash-loops without it. |
| 5 | **A superadmin, created after the migrations** | Until one exists every API route answers **503** (health probes excepted). |
| 6 | **An ingress controller and a TLS certificate**, if anyone outside the cluster is to reach it | The chart ships neither, on purpose. |
| — | *metrics-server (optional)* | Without it the chart's two HPAs report `<unknown>` and never scale. Replicas stay fixed; nothing else breaks. |
| — | ***MySQL only:*** **images built with the database driver** | `--build-arg DB_EXTRAS="[mysql]"`, which `setup-velaris-k8s.sh --build` passes for you. Without it the service cannot open a connection at all — and the pod still reports Ready, so check the logs, not the status. |

**Check which cluster you are about to install into before you start.** The
scripts install into whatever `kubectl config current-context` points at —
that, and nothing else, decides which cluster gets it.

```bash
kubectl config current-context
kubectl get storageclass          # one must be marked (default)
VELARIS_IMAGE_REGISTRY=your.registry/velaris ./K8/MySQL/setup-velaris-k8s.sh --build
```

Longer notes on each — the Temporal/PostgreSQL 18 incompatibility, which five
images are built, why the superadmin cannot run as a Job — are in
`K8/Postgres/README.md` §2 and apply unchanged.

---

## 3. Resource requirements

| Component | Requests | Limits |
|---|---|---|
| Services | 200m / 256Mi | 750m / 768Mi (chart defaults) |
| **MySQL** | **500m / 1Gi** | **2 CPU / 3Gi** (set by the setup script) |

`max_connections` is raised to **500**. Each service pod opens up to 21
connections (10 operations + 5 overflow, 3 auth, 3 analytics), and MySQL's
default of 151 is roughly seven pods. Budget `21 × peak pods + 20`.

---

## 4. Defects found while doing this

All five are in the repository, not in Kubernetes. Each was fixed here.

1. **No service image could reach MySQL.** `aiomysql` is an optional `mysql`
   extra in all four services and absent entirely from the engine. *Fixed:* an
   opt-in `DB_EXTRAS` build arg in all five Dockerfiles; the default build is
   unchanged.
2. **The chart never told the app which backend it was using.** It set
   `VELARIS_CASE_DATABASE_URL` only, and `resolve_async_url()` aborts startup
   when the URL scheme and the configured backend disagree — so every service
   crash-looped with *"scheme 'mysql' does not match backend 'postgresql'"*.
   *Fixed:* `helix.databaseBackend` in `_helpers.tpl` and
   `VELARIS_CASE_DATABASE_BACKEND` on all five deployments, derived from the
   same switch as the URL so the two can never disagree.
3. **Analytics used PostgreSQL-only SQL.** `extract("epoch", …)` renders as
   `EXTRACT(epoch FROM …)`, which MySQL rejects with a syntax error — a hard 500
   on `/analytics/overview` for every MySQL install. *Fixed:* new
   `velaris_shared/db/portable_sql.py`; both call sites now use
   `duration_seconds()`, which emits `TIMESTAMPDIFF` off PostgreSQL.
4. **The database was created utf8mb3**, so any 4-byte character — an emoji in a
   case field, extended CJK — came back as
   `VEL-DB-UNAVAILABLE-503`, not a validation error. The app's own MySQL backend
   documents that "utf8mb4 is enforced at schema-creation time"; it is not,
   because the *server* default decides what `CREATE DATABASE` produces.
   *Fixed:* the server charset is set before the database exists. On an existing
   install this needs a converting migration, which has not been written.
5. **Read-after-write was broken** — see §5. Found here, fixed platform-wide.

---

---

## 5. Security

Identical to the PostgreSQL track — see `K8/Postgres/README.md` §5, which
applies unchanged: no `securityContext` on any Velaris workload, engine runs as
root, no NetworkPolicy, no TLS in the ingress template, database password as a
plaintext env var, and a rate limiter that trusts `X-Forwarded-For`
unconditionally. One MySQL-specific note: the database password is passed to the
migration runner through `MYSQL_PWD` rather than the command line, so it does
not appear in the pod's process list.

---

---

## 6. Behaviour under load and failure

Two case-service replicas, no autoscaler (metrics-server absent), MySQL at
2 CPU / 3 GB. Not directly comparable to the PostgreSQL figures in
`K8/Postgres/README.md`, which ran 4–10 replicas under an HPA.

| Test | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| 40 concurrent, 60 s | 374 rps | 92 ms | 178 ms | 513 ms | **0 / 22,447** |
| 100 concurrent, 90 s | 341 rps | 256 ms | 700 ms | 815 ms | **0 / 30,729** |
| 100 concurrent + pod kill + Valkey kill | 276 rps | 225 ms | 793 ms | 3,019 ms | **0 / 30,391** |

Killing a case-service pod and Valkey mid-load cost **no failed requests at
all** — only latency. The suite scored 55/55 again immediately afterwards.

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


### Older engine versions

Verified on **MySQL 8.4.5** and **MariaDB 11.8.2** as well as the newest
charts: 24 migrations, utf8mb4, 56/56 twice each. Older Bitnami charts
refuse a `bitnamilegacy/` image unless
`global.security.allowInsecureImages=true` is set.

---

---

## 7. The commit-timing defect (all engines)

`get_session` is a FastAPI `yield` dependency whose commit runs in the request's
exit stack, which FastAPI unwinds **after the response has been sent**. The API
therefore answered `201 {"id": …}` while the row was still uncommitted.

Measured on MySQL before the fix:

| Scenario | Result |
|---|---|
| create, then immediately GET the id | **2 of 8 returned 404** |
| same, under load | **10 of 12 returned 404** |
| same, with a 0.5 s pause | 0 of 8 |
| hard delete, then GET | **204 then 200** — the deleted row read back |

PostgreSQL has the identical race with a window too small to observe. The defect
was the platform's, not MySQL's; MySQL only made it visible.

*Fixed* by `velaris_shared/db/commit_route.py`: a middleware commits every
session registered for the request between the handler returning and the
response being written. Two traps worth recording:

- A **route class does not work here.** `include_router` re-registers each
  sub-router's routes with `route_class_override=type(route)`, so setting
  `app.router.route_class` reached **0 of the routes** — every one of the ~800
  arrives through `include_router`. Only a live check in a running pod caught it.
- Annotating the dependency `request: Request | None = None` makes FastAPI treat
  it as a response field and **refuse to start**. It must be `Request = None`.

A failed commit now surfaces as a 500 instead of a success reply over data that
was never written — strictly safer than before.

---

## 8. What is not proven

- ~~The worker was disabled for these runs.~~ **Proven 2026-08-25**: with a
  Temporal server deployed, the worker connects
  (`Connected to Temporal … Case worker running on queue: helix-case-service`)
  and the suite stays 55/55 with two worker replicas running on MySQL.
- ~~An external (non-bundled) MySQL.~~ **Proven 2026-08-26**: the external
  path runs the whole migration track from a client pod, refuses a foreign
  database, refuses a populated schema with an empty ledger, and adopts one on
  request.
- Multi-node clusters and MySQL 8.4 (only 9.4 was run here). A real ingress
  with TLS is proven on the PostgreSQL track and is engine-independent.
- Converting an **existing** utf8mb3 install to utf8mb4.

---

## 9. Re-verified 2026-08-26

Clean install on MySQL 9.4.0 at 8 CPU / 16 GB: **58/58 green**, including eight
immediate read-after-write pairs and a hard-delete read-back — the shape that
used to fail 2/8 on MySQL before the commit fix in §7. `update`, `stop`,
`--start`, the adoption guard, the foreign-database refusal and a deliberately
failing migration were all exercised; see `K8/Postgres/README.md` §10 for the
full table, which covers all three engines.

**4-byte UTF-8 was checked end to end**, not just at the server variable: an
emoji, a CJK extension-B character and a ZWJ family sequence written through
the API and read back byte-identical, and a search on an emoji returning 200.

**One MySQL-only trap for anyone writing SQL by hand.** The baseline carries no
column defaults — PostgreSQL has `id UUID … DEFAULT gen_random_uuid()` and
`failed_attempts INT DEFAULT 0`, MySQL has `CHAR(36) NOT NULL` and `INT NOT
NULL` with no default. The application never notices, because SQLAlchemy
supplies both in Python. A hand-written `INSERT` copied from the PostgreSQL
side does: it fails with `Field 'failed_attempts' doesn't have a default
value`. Do **not** reach for `INSERT IGNORE` — it downgrades that to a warning
and writes an empty string as the primary key, which resurfaces much later as
`ValueError: badly formed hexadecimal UUID string` when someone tries to log
in. Name every column.
