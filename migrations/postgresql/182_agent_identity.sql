-- F-00 — the agent principal.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-00. Everything the AI-era roadmap
-- builds on top of this has to be able to say *which agent, which model, which
-- version* did a thing. Today it cannot: HxNexus attributes its calls to the
-- user whose request happened to trigger them, and a marketplace container acts
-- as the synthetic string `app-<row id>` invented at the call site in
-- `marketplace/grants.py`. A decision ledger built over that attributes AI
-- actions to a generic identity, which is worthless as evidence.
--
-- So an agent becomes a first-class principal with its own row, its own
-- capability ceiling, and its own short-lived tokens.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Four new tables, one new namespace
-- row clamped to `shadow` like every other, and no column added to anything an
-- existing code path reads. An install that never creates an agent has no
-- agents, and every subject id in the system stays exactly the string it was.

-- ── agents ──────────────────────────────────────────────────────────────────
--
-- `tenant_id` is NOT NULL, and unlike `arbiter_policies` there is deliberately
-- no platform scope. A policy with no tenant is a *baseline* that tightens
-- everyone; an agent with no tenant would be a principal able to act across
-- every tenant on the box, which is the one primitive this table must not
-- offer. A platform-wide worker gets one agent per tenant it serves.
CREATE TABLE IF NOT EXISTS agents (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- internal      — a Velaris worker (HxNexus retrieval, HxSentinel scoring).
    -- marketplace   — an installed package acting under its capability grant.
    -- external      — a third-party agent holding a case-scoped work order.
    --
    -- All three are in the CHECK because the *schema* is the stable part; which
    -- kinds may actually be ISSUED a token is an application decision, and this
    -- build issues internal and marketplace only. Writing the constraint now
    -- means F-09 adds a code path, not a migration on a live table.
    kind           VARCHAR(20)  NOT NULL,

    -- Stable identifier the code refers to. `hxnexus.retrieval` rather than a
    -- UUID, so a call site can resolve its own agent without storing an id in
    -- config — and tenant-unique, so two tenants' workers are two principals.
    slug           VARCHAR(128) NOT NULL,
    display_name   VARCHAR(255) NOT NULL DEFAULT '',

    -- The model this agent is DECLARED to run. Not the attribution: the actual
    -- `model_id@version` is stamped per call by the chokepoint, because an
    -- agent's binding can change between two calls and the ledger must record
    -- what actually answered, never what was configured.
    model_binding  VARCHAR(255) NULL,

    -- marketplace kind only: the package this agent IS. Left as the same
    -- VARCHAR the marketplace tables use rather than a FK, because
    -- `marketplace_capability_grants.package_id` is itself an unconstrained
    -- string and a FK to nothing is worse than an honest column.
    package_id     VARCHAR(255) NULL,

    -- active | suspended | retired. Suspension is reversible and instant;
    -- retirement is final. Both are checked at token issue AND on every call,
    -- so suspending an agent kills its outstanding tokens without waiting for
    -- them to expire.
    status         VARCHAR(20)  NOT NULL DEFAULT 'active',

    created_by     UUID         NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_agents_kind   CHECK (kind IN ('internal', 'marketplace', 'external')),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'suspended', 'retired')),
    CONSTRAINT uq_agents_tenant_slug UNIQUE (tenant_id, slug)
);

CREATE INDEX IF NOT EXISTS ix_agents_tenant_status ON agents (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_agents_package       ON agents (package_id);


-- ── capability ceiling ──────────────────────────────────────────────────────
--
-- What this agent may EVER be granted a token for. A token carries a subset;
-- it can never carry something absent here, which is the same shape as the
-- manifest permission ceiling added in migration 174 and for the same reason —
-- a ceiling that is validated once and then thrown away is not a ceiling.
--
-- Append-only: a revoked grant keeps its row. "This agent could read cases
-- between March and June" is a question an auditor asks, and a DELETE cannot
-- answer it. Live capabilities are the rows with `revoked_at IS NULL`.
CREATE TABLE IF NOT EXISTS agent_capabilities (
    id          UUID         PRIMARY KEY,
    agent_id    UUID         NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    capability  VARCHAR(128) NOT NULL,
    granted_by  UUID         NULL,
    granted_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ  NULL,
    revoked_by  UUID         NULL
);

-- No UNIQUE (agent_id, capability): re-granting after a revocation must insert
-- a new row rather than resurrect the old one, or the history above is a lie.
-- Duplicate LIVE rows are harmless — the ceiling is a set, read as one.
CREATE INDEX IF NOT EXISTS ix_agent_caps_agent ON agent_capabilities (agent_id, revoked_at);


-- ── token grants ────────────────────────────────────────────────────────────
--
-- The server-side anchor for a short-lived agent JWT, modelled directly on
-- `mcp_token_grants` (mig 152) because that design was right: the JWT is
-- stateless, this row is the truth, and the transport re-reads it on EVERY
-- call so revocation is instant rather than eventual. Time-validity alone is
-- not revocation.
CREATE TABLE IF NOT EXISTS agent_token_grants (
    id               UUID         PRIMARY KEY,          -- = the JWT's jti
    agent_id         UUID         NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tenant_id        UUID         NOT NULL,

    -- The subset of the ceiling this token actually carries, frozen at mint.
    -- Intersected with the LIVE ceiling on every call, so revoking a capability
    -- narrows every outstanding token immediately.
    capabilities     JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- Case-scoped grants (the F-09 handoff shape, usable now): the token is
    -- valid for this case and no other. NULL means agent-scoped. No FK — a
    -- deleted case must not take the grant's audit trail with it.
    audience_case_id UUID         NULL,

    label            VARCHAR(255) NULL,
    issued_by        UUID         NULL,
    expires_at       TIMESTAMPTZ  NOT NULL,
    revoked          BOOLEAN      NOT NULL DEFAULT FALSE,
    revoked_at       TIMESTAMPTZ  NULL,
    revoked_by       UUID         NULL,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_agent_grants_agent   ON agent_token_grants (agent_id, revoked);
CREATE INDEX IF NOT EXISTS ix_agent_grants_expires ON agent_token_grants (expires_at);


-- ── status history ──────────────────────────────────────────────────────────
--
-- Append-only. `agents.status` says what is true now; this says when it became
-- true and who decided. Suspending an agent is a security event and the row it
-- mutates would otherwise carry no trace of ever having been active.
CREATE TABLE IF NOT EXISTS agent_status_history (
    id          UUID         PRIMARY KEY,
    agent_id    UUID         NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    from_status VARCHAR(20)  NULL,                      -- NULL = creation
    to_status   VARCHAR(20)  NOT NULL,
    reason      TEXT         NOT NULL DEFAULT '',
    changed_by  UUID         NULL,
    changed_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_agent_status_agent ON agent_status_history (agent_id, changed_at);


-- ── the `agent` namespace ───────────────────────────────────────────────────
--
-- default_effect = deny: an agent that no policy speaks about can do nothing
-- once this namespace is ever promoted. That is the roadmap's own security
-- baseline (§ "every feature ships PEPs, never enforcement logic of its own"),
-- and it is safe to write today precisely because max_mode is 'shadow' — the
-- deny is computed and logged and binds nothing until an operator promotes.
INSERT INTO arbiter_namespaces (namespace, default_effect, max_mode, description) VALUES
    ('agent', 'deny', 'shadow', 'Agent principals: what an AI agent may do, and on whose behalf.')
ON CONFLICT (namespace) DO NOTHING;
