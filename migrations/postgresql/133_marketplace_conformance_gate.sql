-- Roadmap #27 — HxTest conformance gate + grandfather window (report §6.5).
--
-- The structural conformance CHECKS already existed (testsuite/conformance.py,
-- deterministic static analysis, no LLM) but were only ever run on demand from
-- the HxTest UI — nothing blocked on the result. This migration adds the state
-- the GATE needs to be enforceable and, critically, the state the grandfather
-- window needs to be honest.
--
-- §6.5 is deliberately asymmetric and that asymmetry is the whole design:
--   · a NEW install of a non-conformant package is refused outright;
--   · an EXISTING install is never delisted, never uninstalled — it earns a
--     `legacy_unverified` badge and, after the window, sorts below packages
--     that passed. No package leaves the marketplace without publisher action.
--
-- The window is STORED, not computed from "whenever the gate was switched on".
-- A derived cutover would silently move every time the flag was toggled or a
-- node restarted, which would either grant a publisher a fresh 60 days or expire
-- them early. Both are wrong; both are invisible until someone is locked out.

CREATE TABLE IF NOT EXISTS marketplace_conformance (
    id              UUID PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    package_id      VARCHAR(255) NOT NULL,
    package_version VARCHAR(64)  NOT NULL,
    -- passed | failed. A row exists only once a run has completed; "no row"
    -- is the third state (never submitted) and is what the window forgives.
    status          VARCHAR(16)  NOT NULL,
    total           INTEGER      NOT NULL DEFAULT 0,
    passed_count    INTEGER      NOT NULL DEFAULT 0,
    failed_count    INTEGER      NOT NULL DEFAULT 0,
    -- Full per-check detail, stored verbatim so a refusal can always be
    -- explained to the publisher without re-running anything.
    checks_json     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    run_id          UUID,                    -- test_runs.id when run via HxTest
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- The gate asks exactly one question: "is there a PASSED row for this package
-- at this version, for this tenant?" — so that is the index.
CREATE INDEX IF NOT EXISTS ix_mkt_conf_lookup
    ON marketplace_conformance (tenant_id, package_id, package_version, status);
-- Newest-first history for the publisher-facing results list.
CREATE INDEX IF NOT EXISTS ix_mkt_conf_pkg_recent
    ON marketplace_conformance (tenant_id, package_id, created_at DESC);


-- Singleton (enforced by ux_mkt_conf_window_singleton below): when the gate
-- went live for this platform, and how long publishers have to catch up.
CREATE TABLE IF NOT EXISTS marketplace_conformance_window (
    id           UUID PRIMARY KEY,
    -- Always 1. Carries no meaning on its own; it exists so the singleton can
    -- be enforced by a plain UNIQUE constraint that is identical on Postgres
    -- and MySQL/MariaDB. Postgres alone could do this with a UNIQUE index on
    -- ((TRUE)), but that expression form is not portable, and a table whose
    -- SHAPE differs per dialect cannot be described by one ORM model.
    singleton    SMALLINT     NOT NULL DEFAULT 1,
    cutover_at   TIMESTAMPTZ  NOT NULL,
    window_days  INTEGER      NOT NULL DEFAULT 60,
    activated_by VARCHAR(255) NOT NULL,
    activated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One window per platform. A second row would make "am I grandfathered?"
-- depend on row order, which is exactly the kind of quiet ambiguity that
-- produces a publisher locked out with no explanation.
CREATE UNIQUE INDEX IF NOT EXISTS ux_mkt_conf_window_singleton
    ON marketplace_conformance_window (singleton);
