-- F-12 (first slice) — process-as-negotiation. The capstone.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-12. Two organisations' Velaris
-- instances agree a shared workflow: each side publishes what it offers and
-- what it requires, a deterministic matcher finds a joint composition, humans
-- on both sides approve, and both sign.
--
-- THE PROPERTY THE WHOLE FEATURE RESTS ON: **no trust is required, only
-- verification.** The matcher is deterministic and runs locally on both sides
-- from the same inputs, so each party can independently confirm that the joint
-- composition is the one that was agreed. There is no trusted third party and
-- no shared database — only two local processes and a message contract at the
-- seam, which F-09's handoff protocol already carries.
--
-- AND THE ONE THAT KEEPS IT SAFE: **deny-overrides survives negotiation by
-- construction.** A negotiated contract can never widen what this platform
-- would allow on its own. `effective_ceiling` is stored as the MINIMUM of what
-- was negotiated and what this side's own boundary permits, so a contract
-- cannot grant what the platform denies — not because a check runs at the right
-- moment, but because the wider number is never written down.
--
-- WHAT IS NOT BUILT, and named rather than implied:
--
--   * **A second live Velaris instance.** The counterparty is exercised as a
--     real Ed25519 signer speaking the protocol, exactly as F-09's was. A
--     genuine instance-to-instance negotiation is not something this build can
--     stand up and prove.
--   * **Counterfactual what-if over past joint cases** during renegotiation.
--     The contract versioning is here (`version`, `supersedes`) so the history
--     exists; replaying a candidate contract across it is F-03's machinery
--     pointed at a new cohort, and is a feature rather than a migration.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Two new tables, and a contract
-- needs an enrolled F-09 counterparty before it can exist at all — so an
-- install taking this update has none.


-- ── the contract two organisations agreed ───────────────────────────────────
CREATE TABLE IF NOT EXISTS joint_contracts (
    id              UUID         PRIMARY KEY,
    tenant_id       UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- The other side, from F-09's registry. A contract with somebody nobody
    -- enrolled cannot be created: the pinned key is what both signatures are
    -- checked against.
    counterparty_id UUID         NOT NULL REFERENCES handoff_counterparties(id) ON DELETE CASCADE,

    name            VARCHAR(255) NOT NULL,
    version         INTEGER      NOT NULL DEFAULT 1,
    -- The contract this one replaces. Renegotiation is a NEW row, never an edit:
    -- what both sides signed must stay exactly as it was signed.
    supersedes      UUID         NULL REFERENCES joint_contracts(id) ON DELETE SET NULL,

    -- ── what each side brought ──────────────────────────────────────────────
    -- Offers are F-11 fragment slugs plus what they provide. Requirements are
    -- the guarantees a side will not proceed without: a data-class ceiling, a
    -- region set, an SLA, and evidence obligations.
    our_offer       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    their_offer     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    our_requirements   JSONB     NOT NULL DEFAULT '{}'::jsonb,
    their_requirements JSONB     NOT NULL DEFAULT '{}'::jsonb,

    -- ── what the matcher produced ───────────────────────────────────────────
    -- The joint composition: which side runs which fragments, in what order,
    -- and what crosses the seam between them.
    composition     JSONB        NULL,
    -- Every clause the matcher checked and how it resolved. Both sides compute
    -- this independently from the same inputs and must get the same answer.
    resolution      JSONB        NULL,
    -- The seam's data-class ceiling AS IT WILL BE ENFORCED: the minimum of what
    -- was negotiated and what this side's own boundary permits. Stored as the
    -- minimum rather than checked against it — a contract cannot grant what the
    -- platform denies because the wider number is never written down.
    effective_ceiling VARCHAR(20) NULL,

    -- The hash both signatures cover. Over the composition and the resolved
    -- terms, not the row: a hash over the row would still match if somebody
    -- edited the composition and left the bookkeeping alone.
    contract_sha256 VARCHAR(64)  NULL,
    our_signature   TEXT         NULL,
    their_signature TEXT         NULL,

    -- draft | matched | unmatchable | approved | shadow | enforced | suspended
    -- | withdrawn
    --
    -- `unmatchable` is not a failure of either side: it means the two sets of
    -- requirements have no joint composition that satisfies both, and saying so
    -- is the useful answer. `shadow` before `enforced` is the SOV lifecycle,
    -- deliberately — a joint process that has never run should not be the first
    -- thing a seam enforces.
    status          VARCHAR(16)  NOT NULL DEFAULT 'draft',
    unmatched_reason TEXT        NULL,

    approved_by     VARCHAR(255) NULL,
    approved_at     TIMESTAMPTZ  NULL,
    enforced_at     TIMESTAMPTZ  NULL,
    -- Either side may suspend unilaterally. It fail-closes the seam to
    -- proposals-queued rather than data-flowing, so a suspension is safe to
    -- press at 3 a.m. without knowing what is in flight.
    suspended_at    TIMESTAMPTZ  NULL,
    suspended_by    VARCHAR(255) NULL,
    suspended_reason TEXT        NULL,

    created_by      VARCHAR(255) NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_contract_tenant ON joint_contracts (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_contract_party  ON joint_contracts (counterparty_id, version);


-- ── the interleaved anchor exchange ─────────────────────────────────────────
--
-- Each side periodically records the OTHER side's chain head, and hands over
-- its own. Neither can rewrite the joint history afterwards without breaking a
-- hash the other is holding — which gives both parties tamper-evidence over a
-- shared story **without a shared ledger and without a third party**.
--
-- Repudiation becomes symmetric: it is not that we can prove what they did, it
-- is that neither of us can deny it.
CREATE TABLE IF NOT EXISTS contract_anchors (
    id            UUID         PRIMARY KEY,
    contract_id   UUID         NOT NULL REFERENCES joint_contracts(id) ON DELETE CASCADE,
    tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- ours | theirs. Whose chain head this row records.
    direction     VARCHAR(8)   NOT NULL,
    seq           INTEGER      NOT NULL,
    chain_head    VARCHAR(64)  NOT NULL,
    -- The signature over the head, by whichever side owns it.
    signature     TEXT         NULL,
    -- Set when a head we were handed does not extend the one we last held.
    -- A rewritten history is exactly what this exchange exists to detect, so
    -- the detection is a row rather than a log line.
    divergence    TEXT         NULL,

    anchored_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_anchor_contract ON contract_anchors (contract_id, direction, seq);
