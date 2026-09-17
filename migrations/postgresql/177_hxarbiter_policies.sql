-- HxArbiter — policies, immutable versions, and the namespace registry.
--
-- Design: docs/Future/Done/HXARBITER-DESIGN.md §10. The columns here are NOT the
-- ones that document sketched; the sketch predated the code and five of its
-- columns could not answer questions the product exists to answer. What
-- changed and why is recorded inline, because the next person to read this
-- will otherwise "fix" it back.
--
-- Everything lands clamped to `shadow`: this engine computes and logs and
-- changes no answer anybody receives until an operator raises a ceiling
-- deliberately. An install taking this update sees no behaviour change at all,
-- which is the only way a policy engine can be added to a running system.


-- ── namespace registry ──────────────────────────────────────────────────────
--
-- Global on purpose. §5.4's prose says defaults are "seeded per tenant", which
-- is wrong about this table and right about the mechanism: a tenant varies a
-- default by writing a POLICY, not by editing the registry. The registry is the
-- global clamp, and a clamp a tenant could raise would not be one.
--
-- This table has no tenant column and that is a deliberate, recorded choice —
-- not the accident that left `variable_namespaces` globally unique.
CREATE TABLE IF NOT EXISTS arbiter_namespaces (
    namespace      VARCHAR(64)  PRIMARY KEY,
    default_effect VARCHAR(10)  NOT NULL,                  -- allow|deny
    -- The global ceiling. Every namespace ships at 'shadow'; raising one is an
    -- operator's decision and never an update's.
    max_mode       VARCHAR(20)  NOT NULL DEFAULT 'shadow',
    description    TEXT         NOT NULL DEFAULT '',
    -- Decision-log volume control (§10). The sketch specified these rates in
    -- prose and gave them nowhere to live, which would have hardcoded them.
    -- Every deny, every obligation-bearing allow and every legacy disagreement
    -- is kept regardless of these; they sample only the uninteresting majority.
    sample_allow_rate   NUMERIC(4,3) NOT NULL DEFAULT 1.000,
    sample_default_rate NUMERIC(4,3) NOT NULL DEFAULT 1.000,
    -- When this namespace left shadow. An auditable fact about the platform
    -- itself, and the thing you want when a decision from last March has to be
    -- explained as binding or not.
    promoted_at    TIMESTAMPTZ  NULL,
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Seeded defaults per §5.4. Sovereignty-bearing namespaces default to DENY, so
-- an unmatched request in them is refused rather than waved through; `case` and
-- `auth` default to ALLOW because base RBAC already gates them and cases must
-- keep flowing. All at max_mode='shadow' — none of this decides anything yet.
INSERT INTO arbiter_namespaces (namespace, default_effect, max_mode, description) VALUES
    ('ai',     'deny',  'shadow', 'AI inference and provider egress. Supersedes HxNexus local_only.'),
    ('egress', 'deny',  'shadow', 'Outbound network calls. Generalises marketplace outbound_domains.'),
    ('export', 'deny',  'shadow', 'Data leaving the platform in any form.'),
    ('vault',  'deny',  'shadow', 'HxVault key operations.'),
    ('case',   'allow', 'shadow', 'Case stage transitions and case-scoped actions.'),
    ('auth',   'allow', 'shadow', 'Session elevation and step-up requirements.')
ON CONFLICT (namespace) DO NOTHING;


-- ── policies ────────────────────────────────────────────────────────────────
--
-- A policy is a logical identity. Its rules live in versions; this row only
-- says which version is currently active.
CREATE TABLE IF NOT EXISTS arbiter_policies (
    id             UUID         PRIMARY KEY,
    -- NULL means platform-level, and that is load-bearing: platform and tenant
    -- policies are evaluated together under deny-overrides (§5.3), so a tenant
    -- can tighten the platform baseline and never loosen it. NULL also matches
    -- what `tenancy.identity.same_tenant()` already treats as platform-shared.
    tenant_id      UUID         NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name           VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL DEFAULT '',
    namespace      VARCHAR(64)  NOT NULL REFERENCES arbiter_namespaces(namespace),
    status         VARCHAR(20)  NOT NULL DEFAULT 'draft',   -- draft|active|retired
    active_version INTEGER      NULL,
    -- `legacy_bridge` marks a policy generated to mirror an existing control
    -- (§8). It matters at retirement: the bridge policy retires when the legacy
    -- control does, not when a human decides it looks redundant.
    origin         VARCHAR(20)  NOT NULL DEFAULT 'user',    -- user|legacy_bridge
    created_by     UUID         NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- When retirement began. §19.6: a retired policy KEEPS EVALUATING in shadow
    -- for a soak period so an operator can see what it would still have caught,
    -- because removing enforcement is not the mirror of adding it. Without this
    -- column there is no way to know when the soak started, or ended.
    retired_at     TIMESTAMPTZ  NULL,
    -- The uniqueness carrier. `tenant_id` is NULL for a platform policy and
    -- NULL <> NULL in SQL, so a UNIQUE over it cannot constrain platform names
    -- at all. This column collapses NULL to the literal 'platform' so ONE
    -- ordinary UNIQUE covers both scopes, identically on all three engines.
    --
    -- It is a plain column and not a generated one on purpose: MariaDB 11.8
    -- refuses COALESCE inside GENERATED ALWAYS AS (error 1901) where MySQL 8.4
    -- accepts it, and a schema that works on two engines out of three is a trap
    -- for whoever adds the fourth. The CHECK below is what keeps it honest —
    -- the writer supplies the value, the database refuses a value that lies.
    tenant_key     VARCHAR(36)  NOT NULL,
    CONSTRAINT ck_arbiter_policies_tenant_key CHECK (
        (tenant_id IS NULL     AND tenant_key = 'platform') OR
        (tenant_id IS NOT NULL AND tenant_key = tenant_id::text)
    )
);

-- The design doc specified UNIQUE (tenant_id, name). It does not do what it
-- reads as doing: proven against a live PostgreSQL, that constraint accepts
-- three identical PLATFORM policies, because tenant_id is NULL for all of them
-- and NULL <> NULL. That is the scope where a duplicate does most damage — a
-- platform policy is the one a tenant cannot override. Uniqueness therefore
-- runs through `tenant_key`, which is never NULL.
CREATE UNIQUE INDEX IF NOT EXISTS ux_arbiter_policies_scope_name
    ON arbiter_policies (tenant_key, name);

CREATE INDEX IF NOT EXISTS ix_arbiter_policies_lookup
    ON arbiter_policies (namespace, status, tenant_id);


-- ── versions ────────────────────────────────────────────────────────────────
--
-- Append-only. A version is never edited; an edit creates the next one.
CREATE TABLE IF NOT EXISTS arbiter_policy_versions (
    policy_id      UUID         NOT NULL REFERENCES arbiter_policies(id) ON DELETE CASCADE,
    version        INTEGER      NOT NULL,
    rules          JSONB        NOT NULL,
    mode           VARCHAR(20)  NOT NULL DEFAULT 'shadow',  -- shadow|dry_run|enforce
    nl_source      TEXT         NULL,
    nl_readback    TEXT         NULL,
    -- NULLABLE, against the design sketch's NOT NULL. A legacy-bridge version
    -- (§8) is generated by the platform and has no human author; forcing a
    -- sentinel user id there would put a fictional person's name on a policy
    -- an auditor reads.
    created_by     UUID         NULL,
    approved_by    UUID         NULL,
    -- OD-4 settles four-eyes on single-admin installs as a COOLING-OFF DELAY
    -- rather than a hard second admin. A delay is unimplementable without the
    -- moment approval happened, which the sketch had nowhere to put.
    approved_at    TIMESTAMPTZ  NULL,
    -- SHA-256 over canonical JSON. Hex, so exactly 64 characters — not TEXT.
    signature      CHAR(64)     NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- THE ACTIVATION INTERVAL, and the single most important correction to the
    -- design sketch. `arbiter_policies.active_version` says what is active
    -- NOW. The product's headline claim is "show me the rule, and prove it was
    -- active on 3 March" — and with only a current pointer that question has no
    -- answer. It cannot be recovered from the decision log either, because §10
    -- samples allow-and-default decisions down to 10% once a namespace is
    -- enforcing, so the evidence would be missing precisely where nothing
    -- interesting happened. These two columns are the audit trail.
    activated_at   TIMESTAMPTZ  NULL,
    deactivated_at TIMESTAMPTZ  NULL,

    PRIMARY KEY (policy_id, version)
);

-- "Which version was active at time T" — the query the claim rests on.
CREATE INDEX IF NOT EXISTS ix_arbiter_versions_active_window
    ON arbiter_policy_versions (policy_id, activated_at, deactivated_at);
