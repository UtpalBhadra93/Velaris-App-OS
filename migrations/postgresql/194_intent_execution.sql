-- F-11 (first slice) — intent-based process execution.
--
-- Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-11. Somebody states an outcome and
-- the platform COMPOSES a path to it from verified fragments, instead of
-- requiring a process drawn end to end in advance.
--
-- THE SECURITY CORE IS CLOSED-WORLD COMPOSITION, and it is the only reason this
-- is safe enough to build at all: **the model never emits a process.** It emits
-- a GOAL SPEC — a small, schema-constrained object naming an outcome and some
-- parameters — and a deterministic planner assembles a path from fragments that
-- were signed, capability-declared and conformance-gated before anybody typed
-- an intent. The same trick as HxArbiter's closed AST and F-08's operator
-- catalogue, scaled up: the generated thing is never the executed thing.
--
-- A fragment is a marketplace-grade artefact. It declares its input and output
-- contracts, the data classes it touches, and the capabilities it needs. Those
-- declarations are the planner's whole vocabulary — a fragment cannot be
-- composed into a plan on the strength of what it actually does, only on what
-- it declared, and the conformance gate is what makes the two agree.
--
-- WHAT IS NOT BUILT, and named rather than implied: **crystallisation** — the
-- one-click promotion of a repeatedly-used composition into a named case type.
-- The plan rows carry what it needs (the fragment sequence and the outcome), so
-- it is a feature rather than a migration, and it is deliberately not built
-- here: a platform that learns processes from usage should not do so in the
-- same release that first learns to compose them at all.
--
-- NOTHING HERE CHANGES BEHAVIOUR ON UPGRADE. Two new tables. With no fragments
-- published, the planner can compose nothing and every intent is refused with a
-- reason, which is exactly what an install taking this update sees.


-- ── a verified building block ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS process_fragments (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug           VARCHAR(64)  NOT NULL,
    display_name   VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL DEFAULT '',

    -- ── the contract, which is the planner's entire vocabulary ──────────────
    -- What must be true before this fragment runs, and what is true after. The
    -- planner matches `provides` to `requires` and nothing else: it has no
    -- opinion about what a fragment means, only about what it declared.
    requires       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    provides       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- Class level only — `personal`, `financial` — never a field name. A
    -- composition's data-class exposure is the union of these, and that union
    -- is what policy is asked about.
    data_classes   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- The side effects this fragment may cause, from a closed vocabulary. A
    -- capability absent here is one the fragment may not use, whatever its
    -- steps look like.
    capabilities   JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- The stages this fragment contributes to a composed definition. Ordinary
    -- `definition_json` stage objects — a composed process runs on the existing
    -- interpreter unchanged, which is the point.
    stages         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    -- How many steps it adds. Kept beside the stages so a plan's total can be
    -- bounded without walking every fragment.
    step_count     INTEGER      NOT NULL DEFAULT 0,

    -- Content-addressed and signed, exactly as a marketplace model package is.
    -- `signed` is not `trusted`: the key id is what an operator pins.
    content_sha256 VARCHAR(64)  NOT NULL,
    signature      TEXT         NULL,
    signing_key_id VARCHAR(64)  NULL,

    -- draft | verified | retired.
    -- Only `verified` fragments may be composed. A draft is a fragment somebody
    -- is still writing; a retired one stays readable because plans that used it
    -- must remain explainable.
    status         VARCHAR(16)  NOT NULL DEFAULT 'draft',
    -- What the conformance gate found: whether the declared capabilities and
    -- data classes actually cover what the stages do.
    conformance    JSONB        NULL,
    verified_at    TIMESTAMPTZ  NULL,
    verified_by    VARCHAR(255) NULL,

    created_by     VARCHAR(255) NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_fragment_slug ON process_fragments (tenant_id, slug);
CREATE INDEX IF NOT EXISTS ix_fragment_status ON process_fragments (tenant_id, status);


-- ── one intent, planned ─────────────────────────────────────────────────────
--
-- The plan is the consent object. A person reads the step list and confirms it,
-- and `plan_sha256` is bound to that confirmation — executing anything whose
-- hash differs is refused. Without that binding, "the user approved a plan"
-- would mean only that they approved *a* plan.
CREATE TABLE IF NOT EXISTS intent_plans (
    id             UUID         PRIMARY KEY,
    tenant_id      UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    case_id        UUID         NULL REFERENCES case_instances(id) ON DELETE SET NULL,

    -- What the person asked for, verbatim, and what it was turned into. Both,
    -- because the goal spec is the only thing the planner saw and the intent is
    -- the only thing the person said — and if they diverge, somebody needs to
    -- be able to see that they did.
    intent         TEXT         NOT NULL,
    goal_spec      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- Whether a model produced the goal spec, or a person supplied it directly.
    -- The planner does not care; a reviewer does.
    spec_source    VARCHAR(16)  NOT NULL DEFAULT 'model',

    -- The composition: the ordered fragment slugs, and the definition they
    -- produce. The definition is stored because it is what would run.
    fragment_slugs JSONB        NOT NULL DEFAULT '[]'::jsonb,
    definition     JSONB        NULL,
    -- The union of the fragments' declarations. What the plan can do and what
    -- it will touch, computed rather than asserted.
    capabilities   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    data_classes   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    step_count     INTEGER      NOT NULL DEFAULT 0,

    -- The hash a confirmation binds to. sha256 over the composed definition.
    plan_sha256    VARCHAR(64)  NULL,

    -- Every rung, in order, with its verdict. Same shape as F-08's ladder, and
    -- for the same reason: a person approving a machine-composed process is
    -- entitled to see what was checked.
    ladder         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    stopped_at     VARCHAR(32)  NULL,
    stopped_reason TEXT         NULL,
    twin_run_id    UUID         NULL,

    -- planned | refused | confirmed | executed | abandoned
    status         VARCHAR(16)  NOT NULL DEFAULT 'planned',
    confirmed_by   VARCHAR(255) NULL,
    confirmed_at   TIMESTAMPTZ  NULL,
    -- The hash AS CONFIRMED. Kept separately from `plan_sha256` on purpose: if
    -- the two ever differ, the plan was changed after a person agreed to it,
    -- and that is a fact worth being able to state rather than infer.
    confirmed_sha256 VARCHAR(64) NULL,
    executed_at    TIMESTAMPTZ  NULL,

    created_by     VARCHAR(255) NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_intent_tenant ON intent_plans (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_intent_case   ON intent_plans (case_id, created_at);
