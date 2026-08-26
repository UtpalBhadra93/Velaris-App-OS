-- HxGuard Phase C (roadmap #16-C) — one authorization generation for the whole
-- platform.
--
-- Service extraction (#13c-e) turned a per-process cache into a correctness
-- problem. Every service runs its own in-process policy decision point with a
-- 30-second decision cache, and `hxguard.invalidate_cache()` clears the cache of
-- **the process that called it**. The mutation routers live in case-service, so
-- revoking an access-group grant or deleting a relationship tuple cleared
-- case-service's cache and left analytics-, hxgraph- and hxmigrate-service
-- serving the old answer until their own entries aged out.
--
-- Nothing carried the fact that a decision input had changed across a process
-- boundary. This row does: a counter every service reads on a short interval.
-- When it moves, that service clears its cache. One small read replaces a
-- message bus, works with Redis switched off (the default), and needs no
-- ordering guarantees — a service that misses an intermediate value still sees
-- a different number and still invalidates.
--
-- Single row by construction: `id` is fixed at 1 with a CHECK, so there is no
-- "which row is the real one" question and an UPDATE is unconditional.
CREATE TABLE IF NOT EXISTS hxguard_authz_generation (
    id          SMALLINT     PRIMARY KEY,
    generation  BIGINT       NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by  VARCHAR(255),
    reason      VARCHAR(100),
    CONSTRAINT ck_hxguard_generation_singleton CHECK (id = 1)
);

-- Seeded here rather than lazily on first write: a service that reads before
-- any mutation has happened must get a number, not an empty result it has to
-- interpret. Zero is a legitimate generation.
INSERT INTO hxguard_authz_generation (id, generation, reason)
VALUES (1, 0, 'seed')
ON CONFLICT (id) DO NOTHING;
