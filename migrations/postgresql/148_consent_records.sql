-- HxConsent — consent & purpose-limitation ledger (§8.1, roadmap #39).
--
-- GDPR Article 15/17 is reactive: it answers "what do you hold on me" after
-- the fact. This is proactive — it records WHY each use of a subject's data
-- was lawful, at the moment it became lawful.
--
-- APPEND-ONLY. A withdrawal is a new row, never an update to the grant. The
-- question a regulator asks is "was this use lawful AT THE TIME", which an
-- overwritten row cannot answer. Current state is derived by reading the
-- latest row per (subject, purpose), so it can never drift from the evidence
-- that backs it.
--
-- `basis` is the GDPR Art. 6 lawful basis and is NOT a synonym for consent:
-- contract, legal obligation and legitimate interest do not require consent
-- and cannot be withdrawn by the subject. Collapsing them into a boolean
-- would misstate the legal position, so the application refuses a withdrawal
-- against a non-withdrawable basis rather than recording a false one.
--
-- Before this, consent existed only as scattered per-feature fields (HxMeet
-- recording, Art. 9 biometric, portal voice) with no ledger and no purpose
-- limitation.

CREATE TABLE IF NOT EXISTS consent_records (
    id           UUID PRIMARY KEY,
    subject_ref  VARCHAR(255) NOT NULL,
    subject_type VARCHAR(32)  NOT NULL DEFAULT 'email',
    purpose      VARCHAR(64)  NOT NULL,
    action       VARCHAR(16)  NOT NULL,
    basis        VARCHAR(32)  NOT NULL DEFAULT 'consent',
    source       VARCHAR(64)  NOT NULL DEFAULT 'api',
    evidence     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    case_id      UUID,
    tenant_id    VARCHAR(64),
    actor_id     VARCHAR(255),
    recorded_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_consent_subject_purpose
    ON consent_records (subject_ref, purpose, recorded_at);
CREATE INDEX IF NOT EXISTS ix_consent_tenant ON consent_records (tenant_id);
