# Velaris on Kubernetes — MariaDB

Running Velaris on Kubernetes against **MariaDB**. MariaDB and MySQL share the
SQLAlchemy driver (`mysql+aiomysql`) and one migration track, so this directory
is the MySQL set with three engine values changed; `K8/MySQL/README.md` is the
fuller reference and everything in it applies unless noted here.

**Verified on** kind v0.32.0 / Kubernetes v1.36.1, Helm v4.2.4, Bitnami mariadb
chart 27.0.7 (**MariaDB 13.0.1**), valkey 6.2.12, node capped at 8 CPU / 16 GB.

**Result: 58/58 functional checks green** on MariaDB (Bitnami chart 27.0.7),
re-verified from a cold start on **2026-08-26** at 8 CPU / 16 GB — see §8. The
55/55 figures below are the 2026-08-25 runs, kept because the load and failure
numbers were measured then.

---

## 1. Scripts

```bash
./K8/MariaDB/setup-velaris-k8s.sh --build --kind my-cluster
./K8/MariaDB/update-velaris-k8s.sh
./K8/MariaDB/stop-velaris-k8s.sh            # --start | --uninstall | --purge
```

Default namespace `velaris-mariadb`. The engine values at the top of
`_common.sh` are `DB_ENGINE=mariadb`, `DB_CLIENT=mariadb`,
`DB_SECRET_KEY=mariadb-password`.

**The SQLAlchemy URL stays `mysql+aiomysql://`.** That is correct, not a
copy-paste slip: SQLAlchemy's dialect for MariaDB genuinely is `mysql`, and
`resolve_async_url()` deliberately compares the *driver scheme* rather than the
backend name so a `mariadb` backend accepts a `mysql://` URL.

### What this track sets

| Setting | MySQL | MariaDB |
|---|---|---|
| Image override | `image.repository=bitnamilegacy/mysql` | `image.tag=latest` |
| Client binary | `mysql` | `mariadb` |
| Secret key | `mysql-password` | `mariadb-password` |

Bitnami's 2025 image move lands differently on the two: MySQL needs the
`bitnamilegacy/` **repository** because its pinned version tag is gone, while
MariaDB has no versioned tag under `bitnamilegacy/` at all but does publish
`bitnami/mariadb:latest`, which matches the subchart's app version. Both were
checked against the registry rather than assumed.

Everything else — utf8mb4 as the server default before the database is created,
`max_connections=500` via `extraFlags` (never `primary.configuration`, which
replaces the whole `my.cnf` and breaks the health probe), the 1Gi/3Gi resource
floor, and `--build-arg DB_EXTRAS="[mysql]"` on the images — is identical.

The engine values at the top of `_common.sh` (`DB_ENGINE=mariadb`,
`DB_CLIENT=mariadb`, `DB_SECRET_KEY=mariadb-password`) and the image override
(`mariadb.image.tag=latest` rather than MySQL's `bitnamilegacy/` repository).
The migration track is shared with MySQL — `migrations/mysql/` — and so is the
driver: **the SQLAlchemy URL stays `mysql+aiomysql://`**, which is correct, not
a copy-paste slip.

---


### Reusing an existing Velaris database

An earlier Velaris install being moved into Kubernetes, a restored dump, or a
database that lives outside the cluster. The bundled database is then not
installed at all:

```bash
VELARIS_K8S_EXTERNAL_URL="mysql+aiomysql://user:pass@host:3306/velaris" \
  ./K8/MariaDB/setup-velaris-k8s.sh
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
| — | ***MariaDB only:*** **images built with the database driver** | `--build-arg DB_EXTRAS="[mysql]"`, which `setup-velaris-k8s.sh --build` passes for you. Without it the service cannot open a connection at all — and the pod still reports Ready, so check the logs, not the status. |

**Check which cluster you are about to install into before you start.** The
scripts install into whatever `kubectl config current-context` points at —
that, and nothing else, decides which cluster gets it.

```bash
kubectl config current-context
kubectl get storageclass          # one must be marked (default)
VELARIS_IMAGE_REGISTRY=your.registry/velaris ./K8/MariaDB/setup-velaris-k8s.sh --build
```

Longer notes on each — the Temporal/PostgreSQL 18 incompatibility, which five
images are built, why the superadmin cannot run as a Job — are in
`K8/Postgres/README.md` §2 and apply unchanged.

---

## 3. Resource requirements

Identical to MySQL — see `K8/MySQL/README.md` §3. The same floor applies
(`mariadb.primary` at 500m/1Gi requests, 2 CPU/3Gi limits, `max_connections=500`
via `extraFlags`), because the subchart's own preset is far too small for a real
workload and its default connection ceiling is below what a handful of pods
open. The connection arithmetic in `K8/Postgres/README.md` §3 is engine-
independent: **21 connections per service pod**.

---

## 4. Defects found while doing this

No MariaDB-specific defect was found. The five in `K8/MySQL/README.md` §4 are
shared, since they live in the chart, the Dockerfiles and the application:
missing driver extras, the missing backend selector, PostgreSQL-only
`EXTRACT(epoch …)` in analytics, the utf8mb3 database, and the platform-wide
commit-timing defect described in `K8/MySQL/README.md` §7.

That MariaDB reached 55/55 on the first attempt is the useful result: it means
those five were genuinely engine-independent, not MySQL quirks.

---

## 5. Security

Identical to the other two tracks — the chart is the same chart. Read
`K8/Postgres/README.md` §5 before a production deployment: no `securityContext`
on the Velaris workloads, no NetworkPolicy, the database password as a plain
environment variable, no gateway (so workload identity is not enforced as it is
on a host install), and a rate limiter that trusts `X-Forwarded-For`.

---

## 6. Behaviour under load and failure

Two case-service replicas, no autoscaler, MariaDB at 2 CPU / 3 GB.

| Test | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|
| 40 concurrent, 60 s | 379 rps | 88 ms | 208 ms | 557 ms | **0 / 22,728** |
| 100 concurrent, 90 s | 343 rps | 262 ms | 676 ms | 784 ms | **0 / 30,828** |
| 100 concurrent + pod kill + **database kill** | 154 rps | 259 ms | 687 ms | 15,031 ms | 809 / 21,515 |

Within noise of MySQL on the first two — 379 vs 374 rps, 343 vs 341.

The third run killed the MariaDB pod itself mid-load. That cost **809 requests**
over the restart (600 client timeouts, 193 × 503, 9 × 500, 7 transport errors),
and then:

- the database came back on its own,
- **no data was lost** — users, cases and the migration ledger all intact,
- the full suite scored **55/55** again with no intervention.

Two operational notes from that run. A suite executed while the database was
still returning fails wholesale with 401s, because login cannot reach the auth
pool — that is the expected shape of the outage, not a separate defect. And a
single database pod remains the platform's one unrecoverable-by-itself
component: the chart runs one replica with no HA on any engine.

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

## 7. What is not proven

The worker **is** proven here: with Temporal deployed it connects and runs on
the `helix-case-service` queue, and the suite stays 55/55 with two worker
replicas. Otherwise as `K8/MySQL/README.md` §8.

---

## 8. Re-verified 2026-08-26

Clean install at 8 CPU / 16 GB: **58/58 green**, including the eight
read-after-write pairs and the 4-byte UTF-8 round trip (emoji, CJK extension B,
ZWJ family — written through the API and read back byte-identical). `update`,
`stop`/`--start`, the adoption guard, the foreign-database refusal, a
deliberately failing migration, and an **external MariaDB** were all exercised.

Two things were proven here rather than on the other tracks:

- **An `update` run after a hand-made `kubectl set image`.** Helm refuses to
  apply over a field another manager owns; the script now explains that and
  prints the recovery, and the printed recovery was followed and worked in one
  shot. The `--field-manager=helm` in it is load-bearing — applying under any
  other name moves the ownership sideways and the next upgrade conflicts again.
- **The worker's missing `imagePullPolicy`** (`K8/Postgres/README.md` §4 item
  7), which only bites on the chart's default `:latest` tag and so had never
  shown up in a run that set `VELARIS_IMAGE_TAG`.

The MySQL note about hand-written `INSERT`s and missing column defaults applies
here unchanged — the two engines share one migration track and one baseline.

Still not exercised: multi-node clusters and older MariaDB releases (11.x,
which the host track used).
