-- Enterprise identity P1 — SAML assertion replay defence.
--
-- A SAML assertion is a bearer credential in a form field. Every control on it
-- is a *time window*: Conditions/NotOnOrAfter and SubjectConfirmationData
-- /NotOnOrAfter are typically five minutes wide, and inside that window the same
-- signed bytes log in as many times as they are posted. Anything that sees the
-- POST once — a proxy log, a browser extension, a shoulder-surfed devtools tab —
-- can replay it.
--
-- The assertion ID is the only thing that makes one assertion distinguishable
-- from another, so the defence is a UNIQUE insert on it: the first POST takes
-- the row, every later one collides and is refused. The PRIMARY KEY is doing the
-- work here, not a SELECT-then-INSERT, which would race exactly when it matters.
--
-- Rows are disposable once `not_on_or_after` has passed: the time window has
-- already closed the door by then, so the table only ever needs to hold live
-- assertions. The retention runner GCs on that column.

CREATE TABLE IF NOT EXISTS saml_assertion_replay (
    assertion_id    VARCHAR(255) PRIMARY KEY,
    provider_id     UUID         NOT NULL,
    not_on_or_after TIMESTAMPTZ  NOT NULL,
    seen_at         TIMESTAMP(6) NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_saml_replay_expiry
    ON saml_assertion_replay(not_on_or_after);

COMMENT ON TABLE saml_assertion_replay IS
    'One row per consumed SAML assertion ID. The UNIQUE primary key IS the '
    'replay defence — a second POST of the same assertion collides and is '
    'refused. GC by not_on_or_after; a row past its window is inert.';

COMMENT ON COLUMN saml_assertion_replay.not_on_or_after IS
    'The assertion''s own expiry. After this instant the row may be deleted: '
    'the assertion is refused on its time window regardless.';
