-- F-09 (first slice) — the agent-to-agent case handoff protocol.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-09. An external AI agent — a
-- partner's Velaris instance, a customer's agent, a third-party service — is
-- granted a CASE-SCOPED work order: accept, read, propose, hand back. Every
-- action ledgered, every action policy-gated, exactly like an internal agent.
--
-- Migration 182 built F-00 agent identity and deliberately left `external` out
-- of the kinds this build issues, saying: *"F-09 adds the code path that admits
-- the third, and it will be a code change with a review, not a migration on a
-- live table."* This is that review.
--
-- THE THREE PROPERTIES THAT MAKE THIS SAFE:
--
--   1. **Unsolicited agents cannot connect.** A counterparty is a row somebody
--      created, carrying a PINNED public key. There is no discovery, no
--      registration endpoint and no "trust on first use" — a handoff to a party
--      nobody enrolled cannot be minted at all.
--   2. **Projection, never redaction-after-send.** The external agent receives
--      a case view COMPUTED from its data-class ceiling. Fields above the
--      ceiling are not sent and then trusted to be ignored; they never leave the
--      process. `handoff_actions.projection` records what was actually
--      disclosed, so a dispute is settled by reading the row.
--   3. **Proposals, not writes.** The protocol has no verb that changes a case.
--      An external agent's output is a proposal a human confirms, through the
--      path F-06 already built for the same reason: its content is untrusted
--      input, and untrusted input may not reach case state on its own.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Three new tables. Handoffs need an
-- enrolled counterparty and an issued work order, so an install taking this
-- update has no counterparties, no orders, and an endpoint that refuses
-- everything.


-- ── who may ever connect ────────────────────────────────────────────────────
--
-- A counterparty is an ORGANISATION, not a session. Its identity is a public
-- key an operator pinned out of band — exactly how a SAML SP pins an IdP
-- certificate — because a key that arrives with the message it authenticates
-- proves only that the sender owns a key, which is not a fact about who they
-- are.
CREATE TABLE IF NOT EXISTS handoff_counterparties (
    id            UUID         PRIMARY KEY,
    tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug          VARCHAR(64)  NOT NULL,
    display_name  VARCHAR(255) NOT NULL,

    -- Raw Ed25519 public key, base64. `key_id` is its fingerprint, and is what
    -- an operator checks against what the counterparty told them by another
    -- channel.
    public_key    TEXT         NOT NULL,
    key_id        VARCHAR(64)  NOT NULL,

    -- active | suspended. Suspending is the fast path: it stops new orders AND
    -- refuses every action on the ones outstanding, so an incident does not
    -- have to be resolved order by order.
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',

    -- The ceiling this counterparty may EVER be granted, independent of any one
    -- work order. A dispatcher issuing an order cannot exceed it, so the
    -- decision "what may this organisation ever see" is made once, by whoever
    -- enrolled them, rather than repeatedly under time pressure.
    max_ceiling   VARCHAR(20)  NOT NULL DEFAULT 'internal',
    -- Free-form: the contract reference, the region, who to ring.
    notes         TEXT         NULL,

    created_by    VARCHAR(255) NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_handoff_cp_slug ON handoff_counterparties (tenant_id, slug);
CREATE INDEX IF NOT EXISTS ix_handoff_cp_key ON handoff_counterparties (key_id);


-- ── one case, one counterparty, one window ──────────────────────────────────
--
-- The work order IS the authority. Not a session, not a login: a case-scoped,
-- expiring, instantly revocable grant, carried by an F-00 agent token so that
-- revocation, the capability ceiling and the deployment kill switch are the
-- ones already built rather than a second set that could disagree.
CREATE TABLE IF NOT EXISTS handoff_work_orders (
    id              UUID         PRIMARY KEY,
    tenant_id       UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id         UUID         NOT NULL REFERENCES case_instances(id) ON DELETE CASCADE,
    counterparty_id UUID         NOT NULL REFERENCES handoff_counterparties(id) ON DELETE CASCADE,
    -- The F-00 agent principal, kind `external`. Every ledger row, every policy
    -- decision and every attribution goes through it, so an external agent is
    -- accounted for in exactly the same places an internal one is.
    agent_id        UUID         NULL,

    -- none | internal | personal. `special_category` is NOT a value: Article 9
    -- data is never projected to an external party by this protocol, and making
    -- it inexpressible is stronger than making it refusable.
    ceiling         VARCHAR(20)  NOT NULL DEFAULT 'internal',
    -- The verbs this order permits, from a closed set. A verb absent here is
    -- refused even if the token would allow it.
    allowed_actions JSONB        NOT NULL DEFAULT '[]'::jsonb,
    obligations     JSONB        NULL,

    -- issued | accepted | returned | revoked | expired.
    -- `returned` is the counterparty handing the case back; `revoked` is us
    -- taking it away. They are different events and a reader needs to know
    -- which happened.
    status          VARCHAR(16)  NOT NULL DEFAULT 'issued',
    expires_at      TIMESTAMPTZ  NOT NULL,

    -- The grant behind the token. Revoking the grant kills the order's
    -- authority instantly, everywhere, without this table being consulted.
    token_grant_id  UUID         NULL,

    -- Our signature over the order, and theirs over the acceptance. Both kept:
    -- together they are a cross-organisation non-repudiation trail that neither
    -- side can rewrite alone.
    order_signature      TEXT    NULL,
    acceptance_signature TEXT    NULL,
    accepted_at     TIMESTAMPTZ  NULL,
    returned_at     TIMESTAMPTZ  NULL,
    return_summary  TEXT         NULL,
    revoked_at      TIMESTAMPTZ  NULL,
    revoked_reason  TEXT         NULL,

    created_by      VARCHAR(255) NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_handoff_wo_case   ON handoff_work_orders (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_handoff_wo_tenant ON handoff_work_orders (tenant_id, status);
-- One active handoff per order, and one active order per case: two external
-- parties working the same case at once is a merge conflict nobody asked for.
CREATE UNIQUE INDEX IF NOT EXISTS ux_handoff_wo_open
    ON handoff_work_orders (case_id) WHERE status IN ('issued', 'accepted');


-- ── every action an external party took ─────────────────────────────────────
--
-- Append-only. `projection` records WHAT WAS ACTUALLY DISCLOSED on a read —
-- the field names, never their values — because "what did they see" is the
-- question a data-protection incident turns on, and reconstructing it from a
-- ceiling and a case that has since changed is guesswork.
CREATE TABLE IF NOT EXISTS handoff_actions (
    id              UUID         PRIMARY KEY,
    work_order_id   UUID         NOT NULL REFERENCES handoff_work_orders(id) ON DELETE CASCADE,
    -- accept | read | propose | return. Closed, and there is deliberately no
    -- verb that writes to a case.
    kind            VARCHAR(16)  NOT NULL,
    -- allowed | denied. A denial is a row: a counterparty repeatedly reaching
    -- for something it may not have is a signal, and one that leaves no trace
    -- is a signal nobody receives.
    decision        VARCHAR(10)  NOT NULL,
    reason          TEXT         NULL,

    -- Field names disclosed, and the names withheld with why. Names only.
    projection      JSONB        NULL,
    -- SHA-256 of what they sent us. The content itself lives on the proposal.
    payload_sha256  VARCHAR(64)  NULL,
    proposal_id     UUID         NULL,
    ledger_record_id UUID        NULL,

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_handoff_act_order ON handoff_actions (work_order_id, created_at);
