-- HxSentinel P2a — deterministic rule layer, findings, and the verify-gate hold.
--
-- P1 built the ground truth (case_outcomes) and the matchable signal index
-- (case_risk_signals). P2a turns those into RECOMMENDATIONS: deterministic,
-- explainable rules that cross-reference a case's signals against the tenant's
-- own labelled history. The engine recommends and cites evidence; a human
-- decides. Nothing here auto-blocks a case.
--
-- Rule DEFINITIONS are a CODE constant (case_service.sentinel.rules), for the
-- same reason the taxonomy is: a rule must mean the same thing everywhere and a
-- constant cannot drift between the PostgreSQL track and the generated MySQL
-- baseline. This migration stores only per-tenant OVERRIDES, the findings the
-- engine produces, and the human-cleared holds the optional gate opens.

-- ── Per-tenant rule config (overrides only) ─────────────────────────────────
-- Absent row = the code default for that rule. A row overrides enablement,
-- severity, and/or threshold params for one rule in one tenant.
CREATE TABLE IF NOT EXISTS case_risk_rules (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(255) NOT NULL,
    rule_key    VARCHAR(64)  NOT NULL,        -- matches a code-constant rule
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    severity    VARCHAR(16),                  -- override low|medium|high (NULL = default)
    params      JSONB,                        -- threshold overrides, e.g. {"window_days":30,"min_cases":3,"z":3.0}
    updated_by  VARCHAR(255) NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_rules_tenant_key
    ON case_risk_rules (tenant_id, rule_key);

-- ── Findings (recommendations) ──────────────────────────────────────────────
-- A finding is a CURRENT assessment, persisted so P2b scores can reference a
-- stable id and the panel can audit exactly what was shown. Idempotent per
-- (case_id, rule_key): re-running the engine SUPERSEDES the prior finding (and
-- marks a no-longer-firing rule's finding 'stale') rather than duplicating it.
-- Not append-only — the provenance is the audit log and the immutable P2b
-- feedback history, not this row.
CREATE TABLE IF NOT EXISTS case_risk_findings (
    id          UUID PRIMARY KEY,
    tenant_id   VARCHAR(255),
    case_id     UUID         NOT NULL,
    rule_key    VARCHAR(64)  NOT NULL,
    severity    VARCHAR(16)  NOT NULL,        -- low | medium | high
    title       VARCHAR(200) NOT NULL,
    message     TEXT         NOT NULL,        -- human-readable recommendation
    evidence    JSONB        NOT NULL,        -- {signal_ids, matched_cases:[{number,outcome}], signal_type, detail}
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',   -- active | stale | dismissed
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_findings_case_rule
    ON case_risk_findings (case_id, rule_key);
CREATE INDEX IF NOT EXISTS ix_risk_findings_tenant
    ON case_risk_findings (tenant_id, severity, status);

-- ── Verify-gate holds (optional, opt-in per tenant) ─────────────────────────
-- A hold is a DISTINCT durable thing, deliberately decoupled from the finding
-- that opened it. When a tenant has set verify_gate_severity and the engine
-- produces a finding at/above that severity, ONE open hold is opened for the
-- case. A later re-evaluation NEVER clears or re-opens a hold — only a human
-- does, by resolving it with a note. This is what keeps the gate honest: a
-- finding going stale must not silently release a case a human hasn't looked
-- at, and a re-firing rule must not re-freeze a case a human already cleared.
--
-- The gate holds a case's stage advance until cleared; it never auto-decides.
-- The advance check FAILS OPEN (a HxSentinel fault must never wedge the core
-- workflow) — a case is held only when an open hold affirmatively exists.
CREATE TABLE IF NOT EXISTS case_risk_holds (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(255),
    case_id         UUID         NOT NULL,
    finding_id      UUID         NOT NULL,    -- the finding that opened it (provenance)
    rule_key        VARCHAR(64)  NOT NULL,
    severity        VARCHAR(16)  NOT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'open',   -- open | cleared | dismissed
    opened_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_by     VARCHAR(255),
    resolved_at     TIMESTAMPTZ,
    resolution_note TEXT
);
-- THE hot read: "does this case have an open hold?" (the advance chokepoint).
CREATE INDEX IF NOT EXISTS ix_risk_holds_case_status
    ON case_risk_holds (case_id, status);

-- ── Per-tenant HxSentinel config ────────────────────────────────────────────
-- One row per tenant, keyed on the SAME normalised tenant string the engine
-- uses everywhere (norm_tenant: a real tenant's id, or 'default'). Kept here
-- rather than in tenant.settings precisely so the gate's tenant key and the
-- findings'/signals' tenant key are the one and same value — no slug/id skew.
-- verify_gate_severity NULL = the optional verify gate is OFF (the default);
-- otherwise it is the minimum finding severity that opens a hold.
CREATE TABLE IF NOT EXISTS case_risk_config (
    tenant_id            VARCHAR(255) PRIMARY KEY,
    verify_gate_severity VARCHAR(16),
    updated_by           VARCHAR(255) NOT NULL,
    updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
