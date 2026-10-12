<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0d9488,100:4ecdc4&height=120&section=header" width="100%" alt="" />

<img src="studio/public/velaris.png" width="140" alt="Velaris" />

<h1>Velaris</h1>

<h3><i>Your infra. Your choice. Zero SaaS lock-in.</i></h3>

<a href="https://github.com/Velaris-App-OS/Velaris-App-OS">
  <img src="https://readme-typing-svg.demolab.com?font=DM+Sans&weight=600&size=22&pause=900&color=0D9488&center=true&vCenter=true&width=780&lines=Structured+workflows+%26+real-time+case+tracking;AI-assisted+triage+with+HxNexus;A+live+semantic+knowledge+graph+%E2%80%94+HxGraph;Per-tenant+encryption+%26+secrets+via+OpenBao" alt="Velaris" />
</a>

<p>
  <img src="https://img.shields.io/badge/License-BSL%201.1%20%E2%86%92%20Apache%202.0-0d9488?style=for-the-badge" alt="License" />
  <img src="https://img.shields.io/badge/Deploy-Self--hosted-4ecdc4?style=for-the-badge" alt="Self-hosted" />
  <img src="https://img.shields.io/badge/Use-Free-26de81?style=for-the-badge" alt="Free to use" />
</p>

</div>

<h3 align="center">Tech Stack</h3>

<div align="center">

![Ollama](https://img.shields.io/badge/Ollama-000000?logo=ollama&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-D97757?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-412991?logo=openai&logoColor=white)
![Gemini](https://img.shields.io/badge/Google_Gemini-8E75B2?logo=googlegemini&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-F55036?logoColor=white)
![Mistral](https://img.shields.io/badge/Mistral-FA520F?logo=mistralai&logoColor=white)
![DeepSeek](https://img.shields.io/badge/DeepSeek-4D6BFE?logo=deepseek&logoColor=white)

![React](https://img.shields.io/badge/React-20232A?logo=react&logoColor=61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-646CFF?logo=vite&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-06B6D4?logo=tailwindcss&logoColor=white)
![D3.js](https://img.shields.io/badge/D3.js-F9A03C?logo=d3dotjs&logoColor=white)

![Python](https://img.shields.io/badge/Python_3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-D71F00?logo=sqlalchemy&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-E92063?logo=pydantic&logoColor=white)

![Temporal](https://img.shields.io/badge/Temporal-000000?logo=temporal&logoColor=white)
![BPMN 2.0](https://img.shields.io/badge/BPMN-2.0-FF6F00)

![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![pgvector](https://img.shields.io/badge/pgvector-4169E1?logo=postgresql&logoColor=white)
![Valkey](https://img.shields.io/badge/Valkey-2A6DB2?logo=valkey&logoColor=white)

![OpenBao](https://img.shields.io/badge/OpenBao-secrets-FFCF25?logo=openbao&logoColor=black)
![JWT](https://img.shields.io/badge/JWT_RS256-000000?logo=jsonwebtokens&logoColor=white)
![WebAuthn](https://img.shields.io/badge/WebAuthn_Passkeys-3423A6?logo=webauthn&logoColor=white)

![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![nginx](https://img.shields.io/badge/nginx-009639?logo=nginx&logoColor=white)
![uv](https://img.shields.io/badge/uv-DE5FE9?logo=python&logoColor=white)
![npm](https://img.shields.io/badge/npm-CB3837?logo=npm&logoColor=white)
![Locust](https://img.shields.io/badge/Locust-2EAE4E?logo=locust&logoColor=white)

</div>

<p align="center">
  <b>Enterprise case management and process automation platform.</b><br/>
  Velaris is a production-grade BPM and case management platform built for operations teams that need
  structured workflows, real-time case tracking, AI-assisted triage, and deep integration capabilities
  &mdash; without SaaS lock-in.
</p>

---

## What's new in 3.1.2

- **A patch for the update path.** MinIO's images left Docker Hub in September 2026; an update then stopped at the image pull with the new code checked out and its migrations unapplied, and said nothing. The updater now survives a failed pull, reports a stopped update, and finishes one when run again.
- **The disk-reclaim step no longer ends an update.** It asked Docker for a field that does not exist, and stopped the update before the services were restarted; a first cut of this patch (v3.1.1) was withdrawn the same day for it.
- **Documents on local disk by default** for a new install, encrypted at rest; object storage is any S3-compatible endpoint you supply. An install that already runs the bundled MinIO keeps it, untouched. `scripts/storage-migrate.py` copies a bucket to disk for those who want to leave it.
- **The Studio update banner** says when the server has no update agent, and stays up after a stopped update. Full notes: [`releases/notes/v3.1.2.md`](releases/notes/v3.1.2.md).

## What's new in 3.1.0

- **A minor: nothing removed or renamed, nothing switches itself on.** What changes on upgrade is who may do what, and only on a multi-tenant install — a non-default tenant's administrator now sees and acts on their own tenant only. A single-tenant install sees no change. Full notes: [`releases/notes/v3.1.0.md`](releases/notes/v3.1.0.md).
- **Legal holds** — freeze a case's records while a dispute is open: nothing may delete, anonymise, erase or move them until the hold is released, and the case can still be worked.
- **Staged rollouts** — a new case type version can go out behind a feature flag, to a percentage of customers, blue-green, in shadow or as an A/B comparison, chosen per deployment and approved by the target environment's operator.
- **Rep desk** — a join link that brings a second rep into a live contact, the full action rail with every action checked against consent, a profile backfill that proposes and never creates, suggested relationships, and industry rail packs. Plus a one-file customer data export (GDPR Art. 15), by staff or by the customer.
- **Tenant-owned design** — case types, forms, rules, data models and calendars belong to the tenant that creates them; platform defaults are inherited, and an edit makes the tenant's own copy.
- **The tenant boundary, everywhere** — a probe that calls every route of every service as another tenant found about 110 routes and 45 lists crossing tenants; one guard now closes them as a class. Script tasks can no longer run arbitrary code; payments, webhooks and 101 login-only routes gained real privileges.
- **Also** — the offline field node, background document indexing on upload, optional structure-aware chunking and reranking for document Q&A, three more privacy switches, an audit chain that still verifies after a retention purge, and an update that now finishes the job on a systemd install.

### 3.0.0

- **A major, for one reason:** six case routes are replaced by one dispatcher, `POST /cases/{id}/actions/{action}`. Anything calling `/status`, `/priority`, `/resolve`, `/close`, `/reopen` or `/cancel` directly gets a 404 — move it before upgrading. Everything else is additive and ships off.
- **HxArbiter** — one deterministic policy engine across every sovereignty and security control, with a tamper-evident decision log, policies drafted in plain English and approved by a person, and every enforcement point in shadow until you promote it.
- **Governed AI, F-00 to F-12** — agents as first-class principals, a hash-chained ledger of every AI decision, cost per decision, re-derivable compliance proofs, a simulation gate and digital twin, an agent workforce, ambient case capture, a sovereign model marketplace, self-healing processes, agent-to-agent handoff, offline field operations (hub), intent-based execution and process-as-negotiation.
- **Customer Engagement** — when someone calls, the rep sees the person, to the extent the caller is verified; floor analytics with stated definitions, suggested next actions a rep can disagree with on the record, and an AI recap in which every point cites its records.
- **Case actions and statuses** — statuses are data a tenant authors; archive, soft delete, escalate, split and transfer; stage-level status and actions; bulk actions including a previewed bulk stage move; and moving a case between tenants with two-person approval and the destination's acceptance.
- **Tenant isolation, audited** — cross-tenant reads of cases, documents and AI answers found and closed at the gate, not per route.
- **Also** — audit streaming to a SIEM, HxShield responses, DMN import/export, local OCR, background document indexing with a queue screen, gateway rate limits on every path, and a Studio that loads six times less on a first visit.

### 2.5.0

- **Four services behind one gateway** — analytics, HxGraph and the migration domain each run on their own; client-facing prefixes are unchanged. The gateway is now the only *caller* as well as the only route: a process opening a service port directly is refused.
- **People Calendar** — absence, availability and team meetings inside HxWork. Leave is a fact and working hours are an assumption: leave decides who owns work, both together answer "when shall we meet". A colleague sees busy, never why. Bring an existing calendar with you: upload an `.ics`, or subscribe to an Outlook/Google/Apple address that is re-read on a timetable — read-only, and only from hosts an administrator has allowed. Out-of-office arrives as a leave *request*, never as approved leave.
- **HxDeploy promotion packages** — frozen, sealed packages promoted between environments with selectable scope, covering bots and schedules, gated by a two-person rule.
- **Bot scheduler and definition editor** — bots configured in Studio, with fan-out schedules and per-bot allow-listed egress. A schedule does not fire until somebody who is not its author approves it.
- **Sign in with Google and GitHub** — two more providers on the login page, with monochrome marks that invert cleanly in dark mode.
- **Enforcement that was described but not performed** — a package's declared permissions are now a ceiling the broker holds it to; HxShield's detector is wired to real traffic instead of an endpoint nobody called; the gateway rate-limits; and a privilege granted in data reaches every service in seconds.
- **Encryption** — AES-256-GCM everywhere credentials are written, tenant-owned secrets under the tenant's own key so erasure makes them unreadable, and no configuration that stores plaintext.
- **Kubernetes, on three databases** — Velaris installs on a cluster you already run, on **PostgreSQL, MySQL or MariaDB**, with one script directory per engine in `K8/`. Point it at a database you already have and the bundled one is not deployed. The standard `setup-velaris.sh` installation is untouched by any of it.
- **Also** — ports are configuration rather than literals, with both the installer and the updater checking before anything binds; an opt-in firewall step that allows ssh first; and 13 named security fixes, each reachable in a running deployment.

### 2.4.0

- **SAML SSO + SCIM provisioning** — proven against live Okta and Entra tenants. Paste one metadata URL and the entity ID, sign-in URL and certificate are read for you; certificates are **add-only**, so a rollover is not an outage, while issuer drift is reported and never applied silently. Velaris can hold its own key pair for signed requests, encrypted assertions and single logout.
- **Omnichannel inbound** — SMS, WhatsApp and CTI arrive as cases. Opt-out is honoured before the provider is called, and the WhatsApp 24-hour window is enforced here rather than at Meta, which accepts a late message and silently never delivers it.
- **Bot Runner (RPA)** — approved, sandboxed automation for systems with no API, with per-bot egress allow-listed rather than open.
- **HxGuard** — the authorization rebuild is finished: every decision is a privilege held **in data**, not a role name in code, and roles map to privileges in a table an administrator can edit.
- **App Builder** — exports one binary serving a client's customers and staff, following the session kind rather than a role name; real login, MFA, keystore and origin-pinned tokens.
- **Also** — Studio browser-storage keys renamed to `velaris_*` without signing anyone out, and stable error codes extended across SAML and the new channels.

### 2.3.0

- **HxConsent** — an append-only record of *why* each use of a subject's data was lawful, at the moment it became lawful. A withdrawal is a new row, because an overwritten one cannot answer "was this lawful **then**". All six GDPR Art. 6 bases; a withdrawal against a non-consent basis is refused. It is also a **control**, not just evidence: marketing email, warehouse egress and AI processing are each refused without a lawful basis.
- **HxPrivacy** — scans customer-built forms for probable personal data. A rescan never overwrites a human verdict; vanished fields go stale rather than deleted.
- **Retention** — enforced, dry-run by default, open cases never deleted, and a platform default defers to any tenant that set its own period.
- **HxLedger** — balanced books. A transaction balances or it does not exist; corrections are reversals, never edits; money is integer cents.
- **HxDR** — recovery posture per region. An untested backup reports `untested`, never green; a restore slower than the objective reports `rto_breached`. `backup-velaris.sh` takes the dump *and* records it.
- **HxStatus** — uptime that never claims what it did not measure.
- **Platform determinism** — event sourcing for the Case aggregate (append-only stream beside the row, with a verify endpoint), a CDC event bus fanning the outbox onto Redpanda, CRDT convergence for offline variable edits, and a Temporal interpreter that runs the existing `definition_json` unchanged.
- **AI governance** — LLMOps observability at the one chokepoint every call passes through; an `ai_action` step where the model *proposes* and a human accepts, edits or rejects; GDPR Art. 22 provenance recording the model that actually answered.
- **Also** — stable error codes in API responses, logs and the docs; statistical SLA prediction; skill-based and manager-of assignment; package format v2 (permissions as ReBAC tuples); and the first frontend tests.

### 2.2.0

- **HxSentinel** — risk that recommends and never decides: ground truth captured before scoring, a verify gate, similarity and oversight, shareable signal packs, a local model trained on the instance's own data, and subject disputes.
- **Voice Input** — sovereign speech everywhere words are typed, transcribe-and-discard by default, voice form-fill, and customer-portal answers on an installed PWA.
- **Corrections** — HxGuard case enforcement defaults to `enforce`; one canonical tenant identity; microsecond timestamps on MySQL/MariaDB; systemd units for engine, case-service and Studio.

### 2.1.0

- **HxMeet** — real-time case sessions: embedded video/screen-share (self-hosted LiveKit) or Teams/Zoom/Meet via connectors; single-use guest invites; consent-gated **sealed recordings and live transcripts** (tenant-key encrypted, audit-chain anchored, verifiable); GPU live captions (Whisper, auto-detected CUDA/Vulkan); post-session AI summaries; document-first KYC verification.
- **HxNexus case Q&A** — ask anything about a case (variables, timeline, messages, documents, transcripts) with cited answers. Sovereignty-first: local models by default, external providers only by explicit tenant consent (pseudonymized + audited).
- **HxNexus Operator (MCP)** — a governed MCP tool surface so external AI agents can read (and, opt-in, write) platform state with scoped tokens and human-confirm proposals.
- **Marketplace** — install apps and connector packages (official / verified / community tiers, out-of-process execution, capability grants).
- **Portal v2** — customer portal rework: offline-capable PWA with queued submissions, worker↔customer messaging, CSAT, customer workflow steps.
- **HxReplay** — counterfactual case replay with cohort what-ifs and case costing (rate cards, timers, billing export).
- **HxEvolve** — process-drift detection that mines deviations and proposes replay-proven, human-approved process changes.
- **HxDBMigrate** — migrate legacy databases: schema analysis, case-type generation, polling sync, one-click cutover with rollback window, signed compliance certificate.
- **HxDraft** — draft-first configuration changes with diff cards and human merge.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Studio (Vite + React)                          :5173           │
│  Case designer · HxGraph visualiser · HxNexus AI · Marketplace  │
│  Test Suite · portals · analytics · HxDeploy · HxWork · HxMeet  │
└───────────────────────────┬─────────────────────────────────────┘
                            │  HTTPS
┌───────────────────────────▼─────────────────────────────────────┐
│  API Gateway (nginx)                            :8200           │
│  The single public door — routes /api/v1/* to case-service      │
└───────────────────────────┬─────────────────────────────────────┘
                            │  loopback
┌───────────────────────────▼─────────────────────────────────────┐
│  Case Service (FastAPI + SQLAlchemy)            :8201           │
│  ├── Cases, stages, steps, assignments, SLA, queues             │
│  ├── Auth (JWT RS256, bcrypt, MFA, passkeys, RBAC + ABAC, SSO)  │
│  ├── HxNexus (multi-provider AI, case Q&A, HxDraft, MCP)        │
│  ├── HxMeet (embedded video, live captions, sealed              │
│  │           recordings & transcripts, document-first KYC)      │
│  ├── HxGraph (in-process knowledge graph)                       │
│  ├── HxVault (per-tenant encryption, crypto-shredding)          │
│  ├── HxDbManager (guarded DB operations)                        │
│  ├── Marketplace + Test Suite / HxTest                          │
│  ├── Customer Accounts + Portal v2 (offline PWA, messaging)     │
│  ├── HxDeploy · HxWork · HxStream · HxShield                    │
│  ├── HxReplay · HxEvolve · HxDBMigrate                          │
│  ├── HxFusion / HxConnect (integrations)                        │
│  ├── HxAnalytics / HxSync / HxGlobal                            │
│  └── Customer portal + multi-tenant + compliance                │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Flow Engine (FastAPI + Temporal)               :8100          │
│  └── BPMN 2.0 process orchestration + durable workers          │
└───────────────────────────┬────────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────────┐
│  Infrastructure (Docker Compose)                               │
│  ├── PostgreSQL 16 (+ pgvector)  :5432                         │
│  ├── Temporal                    :7233  (UI :8233)             │
│  ├── Valkey                      :6379                         │
│  ├── Documents: local disk (S3 endpoint optional)              │
│  ├── OpenBao (secrets manager)                                 │
│  ├── Ollama (local AI backend)   :11434                        │
│  ├── LiveKit SFU + egress (HxMeet video/recording)  :7880      │
│  └── Mailpit (SMTP dev)          :1025  (UI :8025)             │
└────────────────────────────────────────────────────────────────┘
```

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, Shadcn/ui, ReactFlow, D3.js |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2, Alembic |
| API gateway | nginx — single `/api/v1` entrypoint; case-service runs loopback-only behind it |
| Process engine | Temporal, BPMN 2.0 |
| Database | PostgreSQL 16 (+ pgvector) |
| Cache / realtime | Valkey 8 (Redis-compatible) |
| Document storage | Local disk by default, encrypted at rest; any S3-compatible endpoint optional |
| AI (HxNexus) | Ollama (local default), Anthropic Claude, OpenAI, Google Gemini, Groq, Mistral, DeepSeek, Azure — or any OpenAI-compatible endpoint. Sovereignty-first: per-tenant `local_only` (any open-weight model) vs consented external, pseudonymized + audited |
| Real-time sessions (HxMeet) | Self-hosted LiveKit SFU + egress; Whisper live captions (auto-detected GPU: CUDA / Vulkan); recordings & transcripts sealed to the case (tenant DEK + audit-chain anchor) |
| Auth | JWT RS256 (RSA-2048), bcrypt, TOTP MFA, WebAuthn passkeys, RBAC + ABAC, SSO (SAML / OIDC) |
| Secrets | OpenBao — envelope-encrypted, rendered into the app at startup |
| Encryption at rest | HxVault per-tenant DEK (AES-256-GCM, crypto-shredding) |
| Package management | uv (Python), npm workspaces (Node) |
| Load testing | Locust |

---

## Prerequisites

| Requirement | Min version | Notes |
|-------------|-------------|-------|
| Ubuntu / Debian | 22.04+ | Other distros work, untested |
| Docker + Compose | 24+ | `docker compose` v2 plugin |
| uv | 0.4+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node.js | 20+ | LTS recommended |
| npm | 10+ | Ships with Node |
| Python | 3.12 | Managed by uv — no manual install needed |
| Ollama | any | Optional — enables local HxNexus AI features |

---

## Quick start

### 1. First-time setup (run once)

```bash
chmod +x setup-velaris.sh
./setup-velaris.sh
```

This script:
- Installs all Python dependencies into `.venv/` via `uv sync`
- Installs Studio npm dependencies
- Pulls required Docker images
- Runs all database migrations
- Generates infrastructure secrets (managed by OpenBao)
- Validates your Velaris product key
- Creates the superadmin account interactively

### 2. Start all services

```bash
./start-velaris.sh
```

### 3. Open the platform

| Interface | URL |
|-----------|-----|
| Studio (main UI) | http://localhost:5173 |
| API + docs (via gateway) | http://localhost:8200/docs |
| Engine API + docs | http://localhost:8100/docs |
| Temporal UI | http://localhost:8233 |
| Mailpit (dev email) | http://localhost:8025 |

### 4. Update

```bash
./update-velaris.sh
```

Pulls the channel-pinned release, applies any new migrations, and restarts the stack. It takes a
database backup first and aborts if the dump fails.

On an install run by systemd (`deploy/systemd/`), the update finishes with
`deploy/systemd/refresh-after-update.sh`: it re-applies the release manifest, rebuilds Studio and
restarts the units. **Updating *to* 3.1.0 from an earlier release, run that script once by hand
afterwards** — that update is carried out by the older script, which does not know to.

### 5. Back up

```bash
./backup-velaris.sh                    # dump + record it in the HxDR posture
./backup-velaris.sh --keep 30 --quiet  # cron-friendly, prunes dumps older than 30 days
```

Takes the dump **and records it**, in one command. A bare `pg_dump` leaves no record, so the
recovery posture reports *no backup* while backups are in fact being taken — wrong in the direction
that teaches people to ignore the screen. Exits non-zero only if the backup itself failed.

### 6. Stop services

```bash
./stop-velaris.sh          # stops Python services + Studio
./stop-velaris.sh --all    # also tears down Docker (DB, Temporal, etc.)
```

> Stop scripts require your system password. This prevents accidental or unauthorised shutdowns.

### 7. Uninstall

```bash
./uninstall-velaris.sh              # interactive — removes containers, volumes, venv, secrets
./uninstall-velaris.sh --purge      # also remove Docker images and uv
./uninstall-velaris.sh --delete-dir # also remove the project directory (irreversible)
```

---

## Kubernetes (optional)

The quick start above is the standard installation and is unaffected by this
section. If you already run a cluster, Velaris installs on Kubernetes with the
Helm chart in `deploy/helm/velaris` and the scripts in `K8/` — one directory per
database engine:

```bash
./K8/Postgres/setup-velaris-k8s.sh --build   # or K8/MySQL, or K8/MariaDB
./K8/Postgres/update-velaris-k8s.sh
./K8/Postgres/stop-velaris-k8s.sh            # --start | --uninstall | --purge
```

Already running Velaris and moving into a cluster? Point the install at the
database you have and the bundled one is not deployed:

```bash
VELARIS_K8S_EXTERNAL_URL="postgresql+asyncpg://user:pass@host:5432/velaris" \
  ./K8/Postgres/setup-velaris-k8s.sh
```

The target is checked before anything is written: an empty database is
accepted, an existing Velaris database is adopted and only missing migrations
applied, and another application's database is refused.

Each directory has a README covering requirements, resource sizing, the
security posture, and measured load and failure behaviour. Read
`K8/Postgres/README.md` before exposing an install — the chart ships with no
ingress and no TLS by default, on purpose.

## Marketplace — build &amp; use apps

The Velaris Marketplace is an in-app store for extending the platform — connectors, case-type templates, dashboards, prompt packs, portal themes, and full Studio modules — packaged as content-addressed `.hxapp` bundles.

**Using apps.** Browse the catalogue in Studio, review a package's README, screenshots, and declared outbound domains, then install it per tenant in one click. Every install verifies the bundle checksum and validates the manifest first, and you can exercise any app against disposable data in an isolated **sandbox** before it touches production. Uninstall either revokes access (keeping data) or revokes and deletes the app's data.

**Creating apps.** Package your artifacts as an `.hxapp` with a manifest (id, semver `version`, `type`, and the exact `outbound_domains` it is allowed to call). There are two publishing lanes:

- **Official** — published by the Velaris team; a package earns the Official badge only when the source org, the `official/` folder, **and** the built-in registry all agree, so a package can never self-declare its own tier.
- **Community** — open to anyone, via fork + pull request to the marketplace repository.

Either way, a package must pass the **Test Suite conformance gate** — deterministic structural checks (valid manifest, resolvable stage transitions, typed form fields, an acyclic rules graph, no hardcoded tenant/user ids) — before it can be submitted. Third-party code runs **out-of-process only**: installed packages extend Velaris through Studio artifacts and out-of-process connectors, never in-process platform code.

> Marketplace repo: `Velaris-App-OS/Marketplace` (`official/` + `community/` folders). Manage everything from the in-app **Marketplace**; see the docs for publishing details.

---

## Service map

```
services/
├── case-service/          # Active monolith — cases, auth, AI, and all modules
│   ├── case_service/
│   │   ├── api/routers/   # 80+ FastAPI routers
│   │   ├── auth/          # JWT (RS256), RBAC, ABAC, MFA, passkeys
│   │   ├── compliance/    # HxConsent, HxPrivacy, HxLedger, HxDR, retention, audit anchoring
│   │   ├── db/            # SQLAlchemy models + repository
│   │   ├── hxnexus/       # AI assistant (multi-provider)
│   │   ├── hxgraph/       # Knowledge graph
│   │   ├── hxvault/       # Per-tenant encryption (envelope / crypto-shred)
│   │   ├── integrations/  # Background samplers — health, retention, event bus relay
│   │   ├── marketplace/   # App store + trust model + conformance gate
│   │   ├── testsuite/     # Core deterministic test engine
│   │   ├── hxtest/        # AI test generation (marketplace add-on)
│   │   ├── hxdeploy/      # Promotion governance
│   │   ├── middleware/    # Auth, rate limiting, audit, CORS
│   │   └── temporal/      # Workflow activities + workers
│   └── tests/             # Extensive phase_* test suite
├── ai-service/  analytics-service/  audit-service/  connect-service/
├── form-service/  notification-service/  rules-service/
├── user-service/  vcs-service/        # modular-split targets the gateway is pre-wired for
└── …

engine/                    # BPMN process engine (Temporal-backed)
studio/                    # React frontend (Vite)
migrations/                # ordered SQL migrations (postgresql/ 001–217, mysql/ frozen baseline + 155→)
libs/                      # velaris-ir (schema) + velaris-sdk + hxarbiter-core
plugins/                   # auth, cache, db, events, git, integrations, search
load-tests/                # Locust load test suite (4 user classes)
deploy/
├── docker-compose/        # Local dev compose stack (incl. nginx gateway + OpenBao)
├── helm/                  # Kubernetes Helm charts
└── openbao/               # Secrets-manager config
```

> The **API gateway** (nginx) is the only externally exposed door on `:8200`; it routes `/api/v1/*` to the case-service monolith on loopback `:8201`. The gateway's upstreams are pre-wired so analytics, HxGraph, and HxMigrate can split into their own services later with no client change.

---

## Database migrations

Migrations run automatically on `setup-velaris.sh` / `start-velaris.sh`: each file is applied in
filename order and recorded in a `schema_migrations` ledger, so an already-applied file is skipped.
A failing migration stops startup — the platform never boots on a half-applied schema.

```bash
# List migrations (the runner only sees maxdepth 1)
find migrations/ -maxdepth 2 -name "*.sql" | sort

# Apply a specific migration
PGPASSWORD=<pw> psql -h localhost -U velaris -d velaris \
  -f migrations/postgresql/155_dr_measured_rto.sql
```

**PostgreSQL** carries the full incremental history (`001` → `217`). **MySQL/MariaDB** starts from a
**frozen** baseline — `000_baseline_pg154.sql`, a snapshot of PostgreSQL `001`–`154` — followed by
the seed files and then one file per PostgreSQL migration from `155` on, sharing its sibling's
number. The gap between `003` and `155` is intentional: the baseline already contains it.

> **Never regenerate the MySQL baseline to ship a schema change.** On an existing install the
> baseline is already in `schema_migrations`, so a regenerated one is silently skipped and the
> change reaches nobody. Add a numbered file instead. `migrations/mysql/historical/README.md`
> has the full story.

---

## API reference

The platform API is served through the gateway with interactive Swagger UIs:

- **API (via gateway)** — http://localhost:8200/docs (full platform API)
- **Engine** — http://localhost:8100/docs (BPMN process API)

Key endpoint groups:

| Prefix | Description |
|--------|-------------|
| `/api/v1/cases` | Case CRUD, stage transitions, locking |
| `/api/v1/case-types` | Case type designer |
| `/api/v1/auth` | Login, MFA, passkeys, token refresh, user management |
| `/api/v1/my` | Personal assignments, workload, notifications |
| `/api/v1/queues` | Work queues |
| `/api/v1/analytics` | Dashboard, reporting |
| `/api/v1/graph` | HxGraph knowledge graph queries |
| `/api/v1/nexus` | HxNexus AI assistant |
| `/api/v1/marketplace` | Marketplace — discover and install apps |
| `/api/v1/testsuite` | Test Suite — runs, history, conformance gate |
| `/api/v1/hxtest` | HxTest — AI test generation (install-gated add-on) |
| `/api/v1/hxdbmanager` | HxDB Manager — guarded database operations |
| `/api/v1/deploy` | HxDeploy promotion governance |
| `/api/v1/rollouts` | Staged rollouts of case type versions |
| `/api/v1/legal-holds` | Legal holds — place, release, what is frozen |
| `/api/v1/forms` | Form definitions |
| `/api/v1/portal` | Customer-facing portal + customer accounts |

---

## HxGraph (knowledge graph)

Velaris ships a live semantic knowledge graph at `/api/v1/graph`:

```bash
# Natural-language query
curl -s -X POST http://localhost:8200/api/v1/graph/query \
  -H "Content-Type: application/json" \
  -d '{"question":"which modules depend on auth?"}'

# Path between two concepts
curl -s "http://localhost:8200/api/v1/graph/path?from=case&to=stage"

# Interactive visualiser (D3.js)
open http://localhost:8200/api/v1/graph/visualize
```

---

## Load testing

```bash
# Install Locust (already in .venv)
uv run locust --version

# Headless smoke test — 40 users, 2 min
uv run locust -f load-tests/locustfile.py --host http://localhost:8200 \
    --headless -u 40 -r 2 --run-time 2m

# With HTML report
uv run locust -f load-tests/locustfile.py --host http://localhost:8200 \
    --headless -u 40 -r 2 --run-time 5m --html load-tests/report.html

# Interactive web UI (http://localhost:8089)
uv run locust -f load-tests/locustfile.py --host http://localhost:8200
```

Simulated user classes: `CaseWorkerUser` (60%), `ManagerUser` (20%), `DevOpsUser` (10%), `SpikeUser` (10%).

---

## Secrets & configuration

Velaris secrets are managed by **OpenBao** — `setup-velaris.sh` generates them and `start-velaris.sh` renders them into the app at startup (fail-closed: a problem leaves the last-known-good config untouched). There is no manual environment-variable wrangling; for local development an `.env` is created from `.env.example` automatically, and per-tenant data is encrypted at rest by **HxVault**.

---

## License

| Component | License |
|-----------|---------|
| Core platform (`services/`, `engine/`, `studio/`) | Business Source License 1.1 → Apache 2.0 after 4 years |
| SDK + IR schema (`libs/`) | Apache License 2.0 |
| Plugins (`plugins/`) | Apache License 2.0 |

**Free to use.** Run, customise, and build on Velaris for any internal business purpose, and sell services, plugins, and applications built on it. The single BSL 1.1 limit: you may not resell the platform itself, sublicense it, or offer it as a competing hosted/SaaS product. Each release's BSL grant converts to Apache 2.0 after four years. See `LICENSE` for the full terms.

---

## Star History

<a href="https://www.star-history.com/?repos=Velaris-App-OS%2FVelaris-App-OS&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Velaris-App-OS/Velaris-App-OS&type=date&legend=top-left" />
 </picture>
</a>

<div align="center">
  <sub>Questions &middot; <a href="mailto:velaris.app.os@gmail.com">velaris.app.os@gmail.com</a></sub>
  <img src="https://capsule-render.vercel.app/api?type=waving&color=0:4ecdc4,100:0d9488&height=120&section=footer" width="100%" alt="" />
</div>
