-- HxSentinel P5 — optional local risk-scoring model (numpy logistic regression).
--
-- A LOCAL, explainable linear model over the LABEL-INDEPENDENT features only
-- (amounts + identity velocities). Features derived from other cases' outcomes
-- (shares_doc_with_bad, shared_signals_with_bad) are DELIBERATELY excluded —
-- training on them would just launder the deterministic rules' labels back as a
-- fake accuracy. Those stay the province of the rules that cite the actual case.
--
-- The model recommends, never decides. Training produces a PENDING version with
-- holdout metrics; a human ACTIVATES it (never silent), and only one version is
-- active per tenant. Everything runs on-platform (numpy) — no external service.
CREATE TABLE IF NOT EXISTS case_risk_models (
    id            UUID PRIMARY KEY,
    tenant_id     VARCHAR(255) NOT NULL,
    version       INTEGER      NOT NULL,        -- increments per tenant
    coefficients  JSONB        NOT NULL,        -- {feature: weight}
    intercept     DOUBLE PRECISION NOT NULL DEFAULT 0,
    feature_means JSONB        NOT NULL,        -- standardization (train-set means)
    feature_stds  JSONB        NOT NULL,        -- standardization (train-set stds, 0 -> 1)
    threshold     DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    trained_on    INTEGER      NOT NULL,        -- labelled sample count
    metrics       JSONB        NOT NULL,        -- {auc, positives, negatives, holdout}
    status        VARCHAR(16)  NOT NULL DEFAULT 'pending',   -- pending | active | archived
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    activated_by  VARCHAR(255),
    activated_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_risk_models_tenant_version
    ON case_risk_models (tenant_id, version);
-- The hot read: "the active model for this tenant".
CREATE INDEX IF NOT EXISTS ix_risk_models_tenant_status
    ON case_risk_models (tenant_id, status);
