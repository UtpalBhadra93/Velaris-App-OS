-- 201 — HxShield gets a response layer (roadmap #25, market gap C2).
--
-- WHAT WAS MISSING. `hxshield/engine.py::evaluate()` has computed an `action`
-- for every incident since the day it shipped — flag, alert, suspend, block —
-- and **nothing has ever performed one**. The string was returned to a caller
-- that recorded it. Every security-adjacent buyer asks what happens after the
-- alert, and the honest answer was "somebody reads it".
--
-- WHY IT WAITED, AND WHY IT NO LONGER HAS TO. This was blocked on purpose:
-- designing a response layer on a detector that had never fired would have been
-- guesswork. Detection was wired to real traffic on 2026-08-24 and incidents
-- fire, so the responses can be built for the actions the RULES already ask for
-- rather than for actions somebody imagined.
--
-- THE TWO SAFETY PROPERTIES THIS TABLE EXISTS TO HOLD.
--
--  1. **Shadow first.** A response is always RECORDED. It is executed only when
--     the platform switch is on, so an install that takes this update proposes
--     and performs nothing — an operator can read a week of "this is what would
--     have happened" before anything acts. Same posture HxArbiter ships in, for
--     the same reason: an automated response that surprises its operator gets
--     switched off permanently on its first false positive.
--  2. **A destructive response is never automatic.** Suspending an account or
--     killing every session is proposed and waits for a person, whatever the
--     switch says. The approval is on this row, with who and when.
--
-- ADDITIVE, RE-RUNNABLE, and inert on an install that does nothing: no row
-- exists until an incident fires, and no row executes until both gates open.
--
-- Copyright (c) 2024-2026 Velaris Contributors
-- SPDX-License-Identifier: BSL-1.1

CREATE TABLE IF NOT EXISTS shield_responses (
    id             UUID PRIMARY KEY,
    tenant_id      VARCHAR(255) NULL,

    -- The incident that asked for it. Not a foreign key: incidents are subject
    -- to retention and a response must still be answerable for after the
    -- incident row is purged — "why was this account suspended" outlives the
    -- alert that caused it.
    incident_id    UUID         NULL,

    -- The closed vocabulary, in code. A response type this platform has never
    -- heard of must be unrepresentable, not merely unrecognised.
    response_type  VARCHAR(40)  NOT NULL,
    -- Who or what it acts on: a user id, a case id, a grant id.
    target_kind    VARCHAR(40)  NOT NULL,
    target_id      VARCHAR(255) NOT NULL,

    -- proposed | approved | executed | failed | declined | shadowed
    state          VARCHAR(20)  NOT NULL DEFAULT 'proposed',
    -- TRUE for the responses that take something away. Stored rather than
    -- derived so the row still says what it was when the registry changes.
    destructive    BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Why. Carried from the incident so an approver reads the reasoning, not
    -- just the verdict.
    reason         TEXT         NULL,
    patterns       TEXT         NULL,
    score          DOUBLE PRECISION NULL,

    approved_by    VARCHAR(255) NULL,
    approved_at    TIMESTAMPTZ  NULL,
    declined_by    VARCHAR(255) NULL,
    declined_at    TIMESTAMPTZ  NULL,
    executed_at    TIMESTAMPTZ  NULL,
    -- What actually happened, including a failure. A response that silently
    -- did nothing is the failure mode that matters here.
    result         TEXT         NULL,

    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- The queue screen reads "what is waiting for a person", newest first.
CREATE INDEX IF NOT EXISTS idx_shield_responses_state
    ON shield_responses (state, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_shield_responses_incident
    ON shield_responses (incident_id);

-- "Has this actor already been acted on" — asked before every proposal, so a
-- burst of one hundred events does not queue one hundred identical suspensions.
CREATE INDEX IF NOT EXISTS idx_shield_responses_target
    ON shield_responses (target_kind, target_id, created_at DESC);
