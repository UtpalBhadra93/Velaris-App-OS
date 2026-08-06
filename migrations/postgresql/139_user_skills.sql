-- Add `skills` to user_directory — the storage the `skill_based` assignment
-- strategy needs.
--
-- The strategy has been selectable in the Case Designer since the router
-- shipped, but `_resolve_skill_based` was a stub: it logged the requested
-- skills and fell through to the fallback queue. Nothing recorded which user
-- has which skill, so there was nothing to match against.
--
-- A JSON array of plain skill names, e.g. ["claims","fraud"]. Matching is
-- case-insensitive and requires ALL skills named by the rule, so an empty
-- array simply never matches — safe for existing rows, which is why the
-- default is an empty array rather than NULL.

ALTER TABLE user_directory
    ADD COLUMN IF NOT EXISTS skills JSONB NOT NULL DEFAULT '[]'::jsonb;
