-- HxArbiter — make retention and tamper-evidence able to coexist.
--
-- Migration 180 gave the decision log a retention horizon. Running it proved
-- the horizon was unusable: deleting the rows a sealed batch covers makes
-- `verify_chain` fail forever —
--
--     {"ok": false, "problem": "batch 1 sealed 6 decisions and now holds 0"}
--
-- — so switching retention on destroyed the tamper-evidence the product is
-- sold on. The reasoning in 180 was half right: it refused to delete UNSEALED
-- rows because that looks like tampering, and missed that the SEALED ones are
-- the ones whose deletion actually breaks the seal.
--
-- The reconciliation is a tombstone. A batch that has been pruned says so, and
-- keeps its `content_hash` — so the hash CHAIN still links end to end and still
-- proves nothing was edited, while the batch's own content is honestly marked
-- as no longer re-derivable. Deletion costs re-derivability; that is inherent,
-- and the only dishonest option would be to hide it.
--
-- Both columns are nullable with no default, so every existing row reads
-- "never pruned", which is what every existing row is.
ALTER TABLE arbiter_decision_chain
    ADD COLUMN IF NOT EXISTS pruned_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS pruned_count INTEGER     NULL;

-- An auditor's first question after "does it verify" is "what was removed, and
-- when" — that has to be a cheap query, not a scan.
CREATE INDEX IF NOT EXISTS idx_arb_chain_pruned
    ON arbiter_decision_chain (pruned_at);
