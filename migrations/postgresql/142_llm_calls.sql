-- LLMOps observability (#32, report §5.4) — one row per completion.
--
-- Three questions were unanswerable before this table: what is AI costing us,
-- which caller is spending it, and did quality change when the model changed.
-- Nothing recorded a single AI call.
--
-- Rows are written by the platform watcher draining an in-memory queue, not by
-- the caller: the recording point is `GuardedBackend.complete`, the one
-- chokepoint every AI call funnels through, and it sits below the request
-- layer with no session. A dropped metric must never cost a completion.
--
-- `tokens_estimated` is TRUE for every row today — no backend here returns
-- provider usage, so counts are derived from character length. The column
-- exists so a backend that does report usage can write true counts later
-- without a schema change, and so a cost figure is never mistaken for an
-- invoice.

CREATE TABLE IF NOT EXISTS llm_calls (
    id                UUID PRIMARY KEY,
    tenant_id         VARCHAR(255),
    provider          VARCHAR(64)  NOT NULL,
    model             VARCHAR(128) NOT NULL,
    route             VARCHAR(16)  NOT NULL DEFAULT 'direct',
    purpose           VARCHAR(64)  NOT NULL DEFAULT 'completion',
    prompt_tokens     INTEGER      NOT NULL DEFAULT 0,
    completion_tokens INTEGER      NOT NULL DEFAULT 0,
    tokens_estimated  BOOLEAN      NOT NULL DEFAULT TRUE,
    cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    latency_ms        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status            VARCHAR(16)  NOT NULL DEFAULT 'ok',
    error_type        VARCHAR(128),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_llm_calls_created ON llm_calls (created_at);
CREATE INDEX IF NOT EXISTS ix_llm_calls_model   ON llm_calls (model);
