-- HxGlobal — at most one primary region.
--
-- `region_registry.is_primary` was taken straight from the request body and
-- never reconciled against the other rows, so the table could hold two
-- primaries, or — as on the dev install that surfaced this — **none**, while
-- one of the regions is literally named `local-primary`.
--
-- Neither state is harmless. Anything asking "which region is authoritative"
-- has to either guess or refuse:
--
--   * HxDR's ops recorder (`compliance/dr_cli.py`) resolves the region to
--     attach a backup to. With no primary and several regions it declines to
--     guess, and a backup recorded with no region is INVISIBLE to
--     `/dr/posture` — stored, then never shown.
--   * Residency routing (#23, unbuilt) would face the same question.
--
-- Enforced with a PARTIAL unique index rather than a plain one: the constraint
-- is "at most one row where is_primary", and a full unique index on a boolean
-- would instead forbid a second non-primary region, which is nonsense.
--
-- No row is promoted here. Choosing which region is authoritative is an
-- operator's decision about where data lives, and a migration that picked one
-- would be making a residency decision on their behalf.

-- Demote all but the oldest, if several already claim it. Deterministic
-- (oldest wins) so a re-run and a replica converge on the same row rather than
-- on whichever the planner happened to return first.
UPDATE region_registry
   SET is_primary = FALSE
 WHERE is_primary IS TRUE
   AND id <> (
       SELECT id FROM region_registry
        WHERE is_primary IS TRUE
        ORDER BY created_at ASC, id ASC
        LIMIT 1
   );

CREATE UNIQUE INDEX IF NOT EXISTS ux_region_single_primary
    ON region_registry ((TRUE))
    WHERE is_primary IS TRUE;
