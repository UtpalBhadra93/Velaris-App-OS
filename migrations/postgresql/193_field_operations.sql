-- F-10 (first slice) — offline-sovereign field operations, the HUB half.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-10. A field node — a ship, a clinic,
-- a team out of signal — holds a slice of the caseload, is worked on for days
-- disconnected, and converges on reconnection with the sovereignty guarantees
-- still in force.
--
-- WHAT IS BUILT HERE, AND WHAT IS NOT. This migration is the HUB: the node
-- registry and its charter, the dispatch (what a node was given, and what was
-- deliberately withheld), the sub-chain the node keeps while it is away, the
-- merge and its conflict queue, the staleness lockout and the shred order.
--
-- The FIELD NODE ITSELF is not built: a lightweight deployment profile running a
-- case-service subset against SQLite with a local model. That is packaging
-- rather than schema, and a 72-hour partition between two real nodes is not
-- something this build can stand up and prove — so it is named here rather than
-- implied. Every security property the roadmap asks for lives on this side of
-- the link, which is the half that must be right: a node cannot be trusted to
-- enforce anything about itself.
--
-- THE THREAT MODEL IS DEVICE LOSS, and three columns exist only because of it:
--
--   * `charter` — a node holds only what it was given. A stolen node cannot
--     contain what was never dispatched to it, which is a stronger statement
--     than any amount of encryption on a device somebody else is holding.
--   * `ceiling` — the same data-class projection F-09 uses for external agents.
--     A field node is, from the hub's point of view, exactly as untrusted.
--   * `snapshot_max_age_hours` — a node whose policy snapshot has gone stale
--     degrades to DENY rather than carrying on with rules nobody can refresh.
--     Fail-closed on staleness is the only safe direction when the thing that
--     would tell you otherwise is unreachable.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Three new tables. A node must be
-- registered with a pinned key and given a charter before anything happens, so
-- an install taking this update has no nodes and no dispatches.


-- ── a node that may hold work ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS field_nodes (
    id            UUID         PRIMARY KEY,
    tenant_id     UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug          VARCHAR(64)  NOT NULL,
    display_name  VARCHAR(255) NOT NULL,

    -- Pinned exactly as an F-09 counterparty's is, and for the same reason: a
    -- key that arrives with the sync it authenticates proves only that the
    -- sender owns a key.
    public_key    TEXT         NOT NULL,
    key_id        VARCHAR(64)  NOT NULL,

    -- active | suspended | shred_ordered | shredded.
    -- `shred_ordered` is the interesting one: the order is recorded here and
    -- executed at next contact. A node that never comes back is handled by the
    -- staleness lockout instead — the two together are what makes a lost device
    -- survivable.
    status        VARCHAR(20)  NOT NULL DEFAULT 'active',

    -- What this node may EVER hold. Case types, and optionally an explicit case
    -- list; `max_cases` bounds a charter that names only types.
    charter       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- none | internal | personal — F-09's ceilings, deliberately the same
    -- vocabulary. `special_category` is not a value there and is not one here.
    ceiling       VARCHAR(20)  NOT NULL DEFAULT 'internal',

    -- Hours before the node's pinned policy snapshot is too old to act on.
    snapshot_max_age_hours INTEGER NOT NULL DEFAULT 72,

    -- The head of the node's sub-chain as the hub last verified it. A node
    -- resuming from anything other than this is either replaying or has lost
    -- work, and both are conditions a person must see.
    subchain_head VARCHAR(64)  NULL,
    last_contact_at TIMESTAMPTZ NULL,
    shred_ordered_at TIMESTAMPTZ NULL,
    shred_reason  TEXT         NULL,
    shredded_at   TIMESTAMPTZ  NULL,

    created_by    VARCHAR(255) NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_field_node_slug ON field_nodes (tenant_id, slug);


-- ── what a node was handed, and what was withheld ───────────────────────────
--
-- The dispatch is the record that answers "what was on that device" after it
-- has gone missing. It keeps the case ids and the FIELD NAMES disclosed —
-- never the values, which are on the cases themselves.
CREATE TABLE IF NOT EXISTS field_dispatches (
    id             UUID         PRIMARY KEY,
    node_id        UUID         NOT NULL REFERENCES field_nodes(id) ON DELETE CASCADE,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    case_ids       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Per case: what was disclosed and what was withheld, by name.
    projection     JSONB        NULL,

    -- The policy snapshot the node evaluates against while it is away, and its
    -- hash. Pinned rather than fetched, because the whole point is that the
    -- node cannot reach us.
    policy_snapshot      JSONB  NULL,
    policy_snapshot_hash VARCHAR(64) NULL,
    -- The namespaces that must never run disconnected — `export.*` and its
    -- kind. A node applies these locally; the hub records what it told it.
    offline_deny   JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- The sub-chain's genesis. Everything the node records while away hashes
    -- back to this, so a stream that does not start here is not this dispatch's.
    subchain_genesis VARCHAR(64) NOT NULL,

    -- issued | synced | superseded | abandoned
    status         VARCHAR(16)  NOT NULL DEFAULT 'issued',
    issued_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- After this, the node's snapshot is stale and it must fail closed.
    stale_after    TIMESTAMPTZ  NOT NULL,
    synced_at      TIMESTAMPTZ  NULL,

    created_by     VARCHAR(255) NULL
);

CREATE INDEX IF NOT EXISTS ix_field_disp_node ON field_dispatches (node_id, issued_at DESC);


-- ── the events a node did while it was away ─────────────────────────────────
--
-- Append-only on both sides. The node keeps a hash chain; the hub verifies it
-- on arrival, so tampering with a field log breaks a hash the hub is holding.
--
-- `merge_state` is where the roadmap's "deterministic conflict outcomes" and
-- "human reconciliation queue for the irreducible remainder" both live:
-- `applied` merged cleanly, `conflict` needs a person, `rejected` could not be
-- accepted at all (a broken chain, a case outside the charter).
CREATE TABLE IF NOT EXISTS field_sync_events (
    id             UUID         PRIMARY KEY,
    node_id        UUID         NOT NULL REFERENCES field_nodes(id) ON DELETE CASCADE,
    dispatch_id    UUID         NULL REFERENCES field_dispatches(id) ON DELETE SET NULL,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id        UUID         NULL,

    -- The node's own ordering. Gaps are a lost stream, not a late one.
    seq            INTEGER      NOT NULL,
    -- variable_set | note | step_complete. Closed, and deliberately small: a
    -- node may record what a worker did to a case, not what an administrator
    -- did to the platform.
    kind           VARCHAR(24)  NOT NULL,
    target         VARCHAR(255) NULL,
    payload        JSONB        NULL,
    payload_sha256 VARCHAR(64)  NOT NULL,
    -- When the NODE thinks it happened. Never trusted for ordering against hub
    -- events — a disconnected clock is exactly the thing that drifts — but kept,
    -- because it is what a field worker will describe.
    node_time      TIMESTAMPTZ  NULL,

    prev_hash      VARCHAR(64)  NOT NULL,
    content_hash   VARCHAR(64)  NOT NULL,

    -- applied | conflict | rejected
    merge_state    VARCHAR(16)  NOT NULL DEFAULT 'applied',
    conflict_reason TEXT        NULL,
    -- Set when a person decides a conflict. `keep_field` / `keep_hub` — and
    -- nothing decides itself when a `personal` data class is involved.
    resolution     VARCHAR(16)  NULL,
    resolved_by    VARCHAR(255) NULL,
    resolved_at    TIMESTAMPTZ  NULL,

    received_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_field_ev_node  ON field_sync_events (node_id, seq);
CREATE INDEX IF NOT EXISTS ix_field_ev_queue ON field_sync_events (tenant_id, merge_state);
CREATE INDEX IF NOT EXISTS ix_field_ev_case  ON field_sync_events (case_id, received_at);
