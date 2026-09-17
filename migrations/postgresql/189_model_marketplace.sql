-- F-07 (first slice) — the sovereign model marketplace.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-07. A tenant trains a risk model on
-- its own cases; another tenant with the same problem should be able to benefit
-- from that WITHOUT either of them moving a single case. What travels is the
-- learned parameters, never the rows they were learned from.
--
-- What this migration supports:
--
--   BUILT — publishing and installing the two artefacts on this platform that
--   genuinely ARE weights-only: the HxSentinel local risk model
--   (`case_risk_models` — five coefficients, an intercept, five means, five
--   standard deviations and a threshold) and a HxSentinel signal pack
--   (declarative rules, already validated as safe expressions at install).
--   Both are numbers and text. Neither can carry code, and that is a property
--   of the format rather than a promise in a manifest.
--
--   NOT BUILT — sharing fine-tuned LLMs or adapters (GGUF, safetensors, an
--   Ollama install path). The roadmap names them and they are the obvious next
--   step, but this platform has no fine-tuning pipeline, so a package format
--   for weights nothing here can produce would be a schema for a feature that
--   does not exist. The tables below are deliberately artefact-kind-tagged so
--   the day that pipeline exists this is code, not a migration.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Three new tables, nothing altered.
-- No model is published, imported, installed or activated by this migration or
-- by anything that runs on boot. An install taking this update has three empty
-- tables and one new Studio tab that lists nothing.
--
-- **An imported model NEVER scores anything until a person activates it.** The
-- install path writes a `pending` row into `case_risk_models` and stops there:
-- activation stays the human gate HxSentinel P5 already built, and this feature
-- does not get a second one.


-- ── a published or imported package ─────────────────────────────────────────
--
-- One row per artefact this install has either sent out or taken in. Both
-- directions live in one table on purpose: they are the same object seen from
-- two ends, and the questions an auditor asks ("what left here, and what did we
-- take from whom") are the same question with the direction flipped.
--
-- `payload` is the weights themselves. Storing them is what makes the record
-- worth having: a package can be re-served byte-for-byte, a revocation can name
-- exactly what went out, and a dispute about what a model contained is settled
-- by reading the row rather than by trusting a hash of something nobody kept.
CREATE TABLE IF NOT EXISTS model_packages (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- published | imported. Which end of the exchange this row is.
    direction      VARCHAR(16)  NOT NULL,

    -- risk_model | signal_pack. A closed vocabulary, and closed structurally:
    -- an artefact kind with no branch in the export scanner cannot be written
    -- here, so it cannot be published. Widening it takes a migration, which is
    -- the point — every new kind owes an answer to "what can this format
    -- smuggle".
    kind           VARCHAR(20)  NOT NULL,

    -- A stable human name, 'publisher/thing', mirroring the marketplace's own
    -- package ids so the two never need translating between.
    package_id     VARCHAR(255) NOT NULL,
    version        INTEGER      NOT NULL DEFAULT 1,

    -- The content address: SHA-256 over the canonical body (manifest + payload,
    -- sorted keys, no whitespace). It is what the signature covers and what an
    -- importer recomputes; a file whose recomputed hash differs is refused
    -- before anything reads its contents.
    content_sha256 VARCHAR(64)  NOT NULL,

    -- The model card. Class-level only, by rule: intended use, task, the base
    -- artefact, the feature list, the DATA CLASSES the training cohort touched
    -- — never a value from a case.
    manifest       JSONB        NOT NULL,
    -- The weights. Numbers and strings; the export scanner refuses anything
    -- else before this column is ever written.
    payload        JSONB        NOT NULL,

    -- Detached signature over `content_sha256`, and the key that made it.
    -- Unsigned packages are importable ONLY when the operator has said so —
    -- refusing them outright would make this feature unusable on the day it
    -- ships, since no install has ever had a key.
    signature      TEXT         NULL,
    signing_key_id VARCHAR(128) NULL,

    -- What the FILE claims about who made it. Kept separate from `tenant_id`,
    -- which is who holds this row, because a claim and a fact must never be
    -- read off the same column. Nothing on the import side trusts these two.
    publisher_ref  VARCHAR(255) NULL,
    publisher_name VARCHAR(255) NULL,

    -- Where the artefact came from on the publishing side:
    -- 'case_risk_models:<version>' or 'case_risk_signal_packs:<pack_id>'.
    source_ref     VARCHAR(255) NULL,
    -- Cohort size behind the weights. Published because it is the single most
    -- useful number for judging a stranger's model, and because a small cohort
    -- is exactly when the aggregates below stop being aggregates.
    trained_on     INTEGER      NULL,
    -- What the publisher measured on their own holdout. The importer's sandbox
    -- re-measures on THEIR data and compares; a claim nobody re-ran is a claim.
    declared_metrics  JSONB     NULL,
    -- Hash of the feature list + preprocessing the metrics were measured under.
    -- Two models are only comparable when this matches.
    eval_fingerprint  VARCHAR(64) NULL,

    -- ── the two publish gates ───────────────────────────────────────────────
    --
    -- pass | blocked | not_run. The canary probe is a membership-disclosure
    -- check, not a formality: `feature_means` and `feature_stds` ARE summary
    -- statistics of the publisher's own cases, and over a small enough cohort a
    -- mean stops being an aggregate and becomes somebody's claim amount.
    canary_verdict VARCHAR(20)  NOT NULL DEFAULT 'not_run',
    canary_detail  JSONB        NULL,

    -- Class-level data classes the training cohort touched, from HxPrivacy.
    data_classes   JSONB        NULL,
    -- Set when any of those is a special category. A special-category model is
    -- UNPUBLISHABLE — the column records the refusal rather than merely the
    -- policy, so an operator asking "why can we not share this" has an answer.
    special_category BOOLEAN    NOT NULL DEFAULT FALSE,

    -- A named human accepted the residual disclosure risk. Publishing without
    -- it is not possible; the signature says a machine agreed, this says a
    -- person did.
    signed_off_by  VARCHAR(255) NULL,
    signed_off_at  TIMESTAMPTZ  NULL,

    -- draft | published | revoked | quarantined.
    -- `revoked` is the publisher withdrawing; `quarantined` is this install
    -- refusing to install something it already holds. Neither deletes the row:
    -- what a package contained is exactly what somebody will need to read after
    -- it has been withdrawn.
    status         VARCHAR(20)  NOT NULL DEFAULT 'draft',
    status_reason  TEXT         NULL,

    created_by     VARCHAR(255) NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_model_pkg_tenant  ON model_packages (tenant_id, direction, status);
CREATE INDEX IF NOT EXISTS ix_model_pkg_content ON model_packages (content_sha256);
-- Content-addressed, per tenant and direction: re-importing the same file is
-- the same row, not a second opinion about it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_pkg_tenant_content
    ON model_packages (tenant_id, direction, content_sha256);


-- ── one sandbox evaluation of one package ───────────────────────────────────
--
-- The gate. An imported model is scored against the IMPORTING tenant's own
-- labelled holdout before it may be installed, and the result is compared with
-- what the publisher declared. This is a separate table rather than columns on
-- the install because a refusal leaves no install — and a refusal is precisely
-- the record worth keeping.
--
-- It also answers the question the whole feature turns on: a model trained on
-- someone else's distribution may simply not transfer. `measured` is the honest
-- answer for THIS tenant, and it is allowed to be worse than the claim; what is
-- refused is a claim that cannot be reproduced within tolerance.
CREATE TABLE IF NOT EXISTS model_package_evals (
    id             UUID         PRIMARY KEY,
    package_ref    UUID         NOT NULL REFERENCES model_packages(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    holdout_size   INTEGER      NOT NULL DEFAULT 0,
    positives      INTEGER      NOT NULL DEFAULT 0,
    negatives      INTEGER      NOT NULL DEFAULT 0,

    declared       JSONB        NULL,
    measured       JSONB        NULL,
    tolerance      NUMERIC(4,3) NULL,

    -- pass | fail | insufficient_data.
    -- `insufficient_data` is NOT a failure: a tenant with no labelled history
    -- cannot judge a stranger's model, and saying "it failed" would send them
    -- looking for a fault in the package. It is a refusal to install with a
    -- reason, and it is the common case on a young install.
    verdict        VARCHAR(20)  NOT NULL,
    reason         TEXT         NULL,

    evaluated_by   VARCHAR(255) NULL,
    evaluated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_model_eval_pkg ON model_package_evals (package_ref, evaluated_at DESC);


-- ── an imported package placed into this tenant's runtime ───────────────────
--
-- The link between a package and what it BECAME. Without it, an operator
-- looking at an active risk model has no way to learn it came from outside —
-- and "which of our models are ours" is the first question sovereignty asks.
CREATE TABLE IF NOT EXISTS model_package_installs (
    id             UUID         PRIMARY KEY,
    package_ref    UUID         NOT NULL REFERENCES model_packages(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- The attribute HxArbiter policies read. `marketplace` is the only value
    -- this table ever writes; it exists as a column rather than an implication
    -- so a policy can say `model_origin == "marketplace"` about a real fact
    -- instead of inferring it from the presence of a row.
    model_origin   VARCHAR(20)  NOT NULL DEFAULT 'marketplace',

    -- risk_model | signal_pack, and the row it created:
    -- 'case_risk_models:<uuid>' or 'case_risk_signal_packs:<uuid>'.
    target_kind    VARCHAR(20)  NOT NULL,
    target_ref     VARCHAR(255) NULL,

    eval_ref       UUID         NULL REFERENCES model_package_evals(id) ON DELETE SET NULL,

    -- staged | activated | uninstalled | shredded.
    -- `staged` is the resting state and the honest one: install writes a
    -- PENDING model. Activation is HxSentinel's existing human gate and is
    -- recorded here only so this table can answer "is it scoring anything".
    status         VARCHAR(20)  NOT NULL DEFAULT 'staged',

    installed_by   VARCHAR(255) NULL,
    installed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    activated_by   VARCHAR(255) NULL,
    activated_at   TIMESTAMPTZ  NULL,
    uninstalled_at TIMESTAMPTZ  NULL,
    -- Uninstall-with-delete overwrites the package payload and records that it
    -- did. The row survives the weights: the fact that a tenant once ran a
    -- stranger's model is not something a delete button gets to erase.
    shredded_at    TIMESTAMPTZ  NULL,
    shred_reason   VARCHAR(64)  NULL
);

CREATE INDEX IF NOT EXISTS ix_model_install_tenant ON model_package_installs (tenant_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_install_pkg
    ON model_package_installs (tenant_id, package_ref);


-- ── the `model` namespace ───────────────────────────────────────────────────
--
-- default_effect = deny: a model package no policy speaks about may neither be
-- published from here nor score anything here, once this namespace is ever
-- promoted. It is safe to write today precisely because max_mode is 'shadow' —
-- the deny is computed and logged and binds nothing until an operator promotes.
--
-- The namespace exists so a tenant can say the sentence the roadmap asks for:
-- "a model whose `model_origin` is `marketplace` may not score cases carrying
-- `special_category` data". Today that is expressible and shadowed; enforcing
-- it is an operator's decision, never an update's.
INSERT INTO arbiter_namespaces (namespace, default_effect, max_mode, description) VALUES
    ('model', 'deny', 'shadow', 'Model packages: what may be published from here, and whose models may score here.')
ON CONFLICT (namespace) DO NOTHING;
