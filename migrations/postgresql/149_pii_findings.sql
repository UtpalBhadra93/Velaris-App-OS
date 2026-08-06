-- HxPrivacy — automated PII discovery & classification (§8.10, roadmap #39).
--
-- Scans form and data-model definitions to find fields that probably hold
-- personal data, so retention and residency policy can be driven by what is
-- actually collected rather than by what someone remembered to tag. Before
-- this, PII classification existed only as a MANUAL `sensitivity` field on
-- variables and namespaces; nothing discovered anything.
--
-- A finding is a PROPOSAL, not a verdict. Detection is heuristic — field
-- names, labels and declared input types, never values — so every row carries
-- a confidence and the reason it was raised, and a human confirms or dismisses
-- it before it drives policy. That review step matters in both directions: a
-- false positive silently over-restricts a harmless field until something
-- mysteriously breaks, and a false negative leaves personal data untagged
-- while the report looks clean.
--
-- UNIQUE (resource_type, resource_id, field_path) keeps a rescan idempotent —
-- re-running updates a finding instead of stacking duplicates and burying the
-- reviewer's decision. A field that disappears is marked `stale`, never
-- deleted: that personal data WAS collected is itself a fact worth keeping.

CREATE TABLE IF NOT EXISTS pii_findings (
    id               UUID PRIMARY KEY,
    resource_type    VARCHAR(32)  NOT NULL,
    resource_id      UUID         NOT NULL,
    resource_name    VARCHAR(255),
    field_path       VARCHAR(512) NOT NULL,
    field_label      VARCHAR(255),
    category         VARCHAR(32)  NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    reason           TEXT,
    special_category BOOLEAN      NOT NULL DEFAULT FALSE,
    status           VARCHAR(16)  NOT NULL DEFAULT 'detected',
    reviewed_by      VARCHAR(255),
    reviewed_at      TIMESTAMPTZ,
    tenant_id        VARCHAR(64),
    first_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_pii_finding_resource_field
        UNIQUE (resource_type, resource_id, field_path)
);

CREATE INDEX IF NOT EXISTS ix_pii_findings_status ON pii_findings (status);
