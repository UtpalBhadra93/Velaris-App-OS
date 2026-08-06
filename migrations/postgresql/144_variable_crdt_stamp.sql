-- CRDT version stamp on case variables (#38, report §2.7).
--
-- The Portal PWA queued offline submissions with an idempotency key, so a
-- *create* could never duplicate. Offline *edits* had no equivalent: two
-- clients editing the same variable resolved by whichever write arrived last,
-- so the loser's edit vanished silently and the winner depended on network
-- timing and on client clocks that do not agree.
--
-- These two columns make each variable an LWW-Register: merges compare
-- (lamport, origin), never wall time. Lamport gives causality without
-- synchronised clocks; origin breaks ties as a total order, so every replica
-- converges on the same value without coordination.
--
-- Defaults of 0 / '' are the correct starting stamp for existing rows: any
-- genuine incoming edit carries a higher counter and therefore wins, so
-- history is not retroactively frozen.

ALTER TABLE case_instance_variables
    ADD COLUMN IF NOT EXISTS lamport BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS origin VARCHAR(64) NOT NULL DEFAULT '';
