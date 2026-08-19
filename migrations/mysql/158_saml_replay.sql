-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ : SAML assertion replay defence
-- (PostgreSQL migration 158).
--
-- A SAML assertion is a bearer credential in a form field, and every control on
-- it is a *time window* — Conditions/NotOnOrAfter and SubjectConfirmationData
-- /NotOnOrAfter are typically five minutes wide. Inside that window the same
-- signed bytes log in as many times as they are posted, so anything that sees
-- the POST once can replay it.
--
-- The assertion ID is the only thing distinguishing one assertion from another,
-- so the defence is a UNIQUE insert: the first POST takes the row, every later
-- one collides and is refused. The PRIMARY KEY does the work — a
-- SELECT-then-INSERT would race exactly when it matters.
--
-- Rows are disposable once `not_on_or_after` has passed; the time window has
-- already closed the door by then.
--
-- VARCHAR(255) as PK = 1,020 bytes at utf8mb4, inside the 3072-byte InnoDB key
-- limit. CHAR(36) is what `GUID` emits outside PostgreSQL; DATETIME(6) matches
-- `MicroDateTime`.
-- ============================================================================

CREATE TABLE IF NOT EXISTS saml_assertion_replay (
    assertion_id    VARCHAR(255) NOT NULL,
    provider_id     CHAR(36)     NOT NULL,
    not_on_or_after DATETIME     NOT NULL,
    seen_at         DATETIME(6)  NOT NULL,
    PRIMARY KEY (assertion_id),
    KEY ix_saml_replay_expiry (not_on_or_after)
);
