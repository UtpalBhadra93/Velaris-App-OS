-- Item 12 (P0) — Customer Engagement: the identity spine.
--
-- Design: docs/Future/Done/CustomerEngagement.md. The question that started this was
-- a customer asking *"when someone calls, will the rep see the customer's
-- details, and to what extent?"* — and the answer today is no, because Velaris
-- is case-centric and **nothing says these cases, threads, orders, documents and
-- consents belong to the same person.**
--
-- The gap is not a screen. It is an identity, and this migration is it.
--
-- FOUR DECISIONS TAKEN WITH THE USER, 2026-09-04, and each one is visible in the
-- schema rather than only in a document:
--
--   1. **A profile is a PARTY**, not a person — a human, a household or a
--      business — with a party↔party relation table from day one. A person who
--      is a contact on two businesses, or a household with three members, works
--      without a migration on the spine everything else hangs off.
--   2. **All three ownership modes are expressible**, and the marker is per
--      profile with provenance per field. Velaris-owned is the proven path; a
--      CRM-mastered or read-through profile is the same row with a different
--      `source_of_truth` and an `external_crm` identifier. **Nothing about the
--      SPINE is ever external**: which cases belong to one person is a Velaris
--      fact, and no CRM has an opinion about it.
--   3. **We verify, and a caller is Anonymous until they are.** The tier is on
--      the INTERACTION, not the profile — verification is something that
--      happened on a call, not a property somebody acquires permanently.
--   4. **Consent missing on a raise-on-behalf is captured on the call**, not
--      refused. A hard refusal is correct and useless when the rep is talking to
--      the very person whose consent it is.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Six new tables and no alterations.
-- `portal_customers` is **not replaced and not renamed** — it becomes one
-- identifier kind pointing at a profile, and existing portal login keeps working
-- untouched. The feature is off until a tenant is entitled, so an install taking
-- this update has no profiles, resolves nothing, and shows no new screen.


-- ── the party ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_profiles (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- person | household | organisation. A party, not a person — see decision 1.
    party_kind     VARCHAR(16)  NOT NULL DEFAULT 'person',
    display_name   VARCHAR(255) NOT NULL,

    -- active | merged | anonymized.
    -- `anonymized` is a GDPR erasure that keeps the row: the interactions and
    -- links must remain countable, and a deleted profile would silently detach
    -- a call from the case it was about.
    status         VARCHAR(16)  NOT NULL DEFAULT 'active',
    -- A merge is a record, never a deletion. The loser stays readable and
    -- redirects, so an audit can answer "why does this call show that person's
    -- cases".
    merged_into_id UUID         NULL REFERENCES customer_profiles(id) ON DELETE SET NULL,

    -- ── ownership (decision 2) ──────────────────────────────────────────────
    -- velaris | crm_mastered | read_through. Per PROFILE, because a contact
    -- centre normally has both: some customers came from the CRM and some were
    -- created on a call.
    source_of_truth VARCHAR(16) NOT NULL DEFAULT 'velaris',
    -- Which external system, when it is not us. Named rather than assumed:
    -- an install may have two.
    external_system VARCHAR(64) NULL,
    -- Per-FIELD provenance and fetch time: `{"display_name": {"from": "crm",
    -- "at": "..."}}`. A rep reading a name is entitled to know whether it came
    -- from the CRM or from the last conversation, and **staleness is shown,
    -- never hidden** — a cached attribute past its bound is reported as not
    -- refreshed rather than presented as current.
    attribute_source JSONB      NULL,
    -- The last successful refresh from the external system. NULL on a
    -- Velaris-owned profile, and a NULL here must never read as "fresh".
    refreshed_at   TIMESTAMPTZ  NULL,

    -- portal | agent | import | resolution
    source         VARCHAR(16)  NOT NULL DEFAULT 'agent',
    notes          TEXT         NULL,

    created_by     VARCHAR(255) NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_cprofile_tenant ON customer_profiles (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_cprofile_merged ON customer_profiles (merged_into_id);


-- ── how the world names this party ──────────────────────────────────────────
--
-- The index that makes a screen-pop a LOOKUP rather than a scan. Today
-- `channels/cti/pop` reads the last 200 conversations hunting for a number;
-- this is the row that replaces that.
CREATE TABLE IF NOT EXISTS customer_identifiers (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id    UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,

    -- email | phone | portal_customer | external_crm | national_id_hash | account_no
    kind           VARCHAR(24)  NOT NULL,
    -- E.164 for a phone, lowercased for an email. The normalisation is the
    -- whole value of this table: two reps typing the same number differently
    -- must reach the same person.
    value_normalised VARCHAR(255) NOT NULL,
    value_display  VARCHAR(255) NOT NULL,

    -- **Resolution is a suggestion until a human or a challenge confirms it.**
    -- An identifier adopted from an inbound number is `verified = false`, and
    -- nothing about the disclosure tier reads an unverified identifier as proof.
    verified       BOOLEAN      NOT NULL DEFAULT FALSE,
    verified_at    TIMESTAMPTZ  NULL,
    -- otp | call | document | idp
    verified_by    VARCHAR(16)  NULL,

    is_primary     BOOLEAN      NOT NULL DEFAULT FALSE,
    source         VARCHAR(16)  NOT NULL DEFAULT 'agent',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- One value reaches one party. Two profiles claiming the same phone number is
-- the duplicate this whole feature exists to resolve, not something to allow.
CREATE UNIQUE INDEX IF NOT EXISTS ux_cident_value
    ON customer_identifiers (tenant_id, kind, value_normalised);
CREATE INDEX IF NOT EXISTS ix_cident_customer ON customer_identifiers (customer_id, kind);


-- ── party ↔ party (decision 1) ──────────────────────────────────────────────
--
-- A household's members, a business's contacts, an authorised representative.
-- Directional, because "is a contact of" and "has as a contact" are different
-- statements and collapsing them loses which way round the authority runs.
CREATE TABLE IF NOT EXISTS customer_relations (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    from_customer_id UUID       NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    to_customer_id   UUID       NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    -- member_of | contact_for | authorised_by | related_to
    relation       VARCHAR(24)  NOT NULL,
    -- Whether this relation lets one party act for the other. Recorded rather
    -- than inferred from the relation name: a household member is not
    -- automatically allowed to speak for the household.
    may_act_for    BOOLEAN      NOT NULL DEFAULT FALSE,
    note           TEXT         NULL,
    created_by     VARCHAR(255) NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_crel_edge
    ON customer_relations (from_customer_id, to_customer_id, relation);
CREATE INDEX IF NOT EXISTS ix_crel_to ON customer_relations (to_customer_id);


-- ── the party's footprint, without touching the objects ─────────────────────
--
-- Additive and FK-free on the object side ON PURPOSE. A case, a conversation, an
-- order and a document live in different tables with different lifecycles, and
-- adding a customer column to each would be five migrations on live tables plus
-- five places to keep in step.
CREATE TABLE IF NOT EXISTS customer_links (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id    UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    -- case | conversation | order | document | session | interaction
    object_kind    VARCHAR(24)  NOT NULL,
    object_id      UUID         NOT NULL,
    -- resolved | manual | on_behalf. How this link came to exist, because a
    -- link a machine guessed and a link a person made are different evidence.
    link_reason    VARCHAR(16)  NOT NULL DEFAULT 'resolved',
    linked_by      VARCHAR(255) NULL,
    linked_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_clink_object
    ON customer_links (customer_id, object_kind, object_id);
CREATE INDEX IF NOT EXISTS ix_clink_lookup ON customer_links (tenant_id, object_kind, object_id);


-- ── merges, reversible ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customer_merges (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    winner_id      UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    loser_id       UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    -- What moved, so an undo puts back exactly what a merge took.
    moved          JSONB        NULL,
    reason         TEXT         NOT NULL,
    merged_by      VARCHAR(255) NOT NULL,
    merged_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    undone_at      TIMESTAMPTZ  NULL,
    undone_by      VARCHAR(255) NULL
);

CREATE INDEX IF NOT EXISTS ix_cmerge_winner ON customer_merges (winner_id, merged_at);


-- ── what a call IS — the piece Velaris has never had ────────────────────────
--
-- Not a case and not a conversation: the contact itself. It is what makes
-- average handling time, wrap-up codes, "what did we promise last time" and
-- repeat-contact analytics possible, and it is the row a screen-pop creates.
CREATE TABLE IF NOT EXISTS customer_interactions (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    -- NULL is legitimate and important: an unresolved call is still an
    -- interaction. **Never auto-create a profile from an unverified inbound
    -- number** — that manufactures a person out of a wrong number.
    customer_id    UUID         NULL REFERENCES customer_profiles(id) ON DELETE SET NULL,

    -- voice | sms | whatsapp | email | portal | chat | walk_in
    channel        VARCHAR(16)  NOT NULL,
    direction      VARCHAR(8)   NOT NULL DEFAULT 'inbound',
    started_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ended_at       TIMESTAMPTZ  NULL,
    agent_user_id  VARCHAR(255) NULL,

    external_call_id VARCHAR(255) NULL,
    ani            VARCHAR(64)  NULL,
    dnis           VARCHAR(64)  NULL,
    queue          VARCHAR(128) NULL,
    -- The interaction this was warm-transferred from. **A warm transfer carries
    -- the interaction, not just the call**, so the receiving rep inherits the
    -- verification already established — being re-verified is the number one
    -- complaint about every contact centre.
    transferred_from UUID       NULL REFERENCES customer_interactions(id) ON DELETE SET NULL,

    -- none | partial | full. **On the INTERACTION, not the profile**:
    -- verification is something that happened on a call, not a property
    -- somebody acquires permanently.
    verification_level VARCHAR(8) NOT NULL DEFAULT 'none',
    verified_by    VARCHAR(16)  NULL,
    verified_at    TIMESTAMPTZ  NULL,

    disposition_code VARCHAR(64) NULL,
    wrap_up_notes  TEXT         NULL,
    -- open | wrapped | abandoned. An unclosed interaction AGES into `abandoned`;
    -- wrap-up is required by configuration, never by force, because a wrap-up
    -- that blocks the next call is a wrap-up that gets faked.
    status         VARCHAR(16)  NOT NULL DEFAULT 'open',
    outcome        VARCHAR(64)  NULL,

    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_cint_customer ON customer_interactions (customer_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_cint_tenant   ON customer_interactions (tenant_id, status, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_cint_agent    ON customer_interactions (agent_user_id, started_at DESC);


-- ── every field a rep unmasked, and why ─────────────────────────────────────
--
-- **Reveal-on-reason**: a masked field unmasks ONE AT A TIME, each with a
-- reason, each a row. Bulk unmask does not exist — a "show everything" button is
-- how a disclosure tier becomes decoration.
CREATE TABLE IF NOT EXISTS customer_disclosures (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id    UUID         NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    interaction_id UUID         NULL REFERENCES customer_interactions(id) ON DELETE SET NULL,
    field_path     VARCHAR(128) NOT NULL,
    reason         TEXT         NOT NULL,
    -- The tier in force when it was revealed, frozen. Reading it live later
    -- would show what the caller eventually reached, not what they had reached
    -- when the field was shown.
    at_verification VARCHAR(8)  NOT NULL,
    revealed_by    VARCHAR(255) NOT NULL,
    revealed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_cdisc_customer ON customer_disclosures (customer_id, revealed_at DESC);
CREATE INDEX IF NOT EXISTS ix_cdisc_agent    ON customer_disclosures (revealed_by, revealed_at DESC);
