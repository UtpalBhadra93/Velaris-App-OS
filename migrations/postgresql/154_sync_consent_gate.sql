-- HxConsent becomes a control for `data_sharing`, at the HxSync egress.
--
-- Until now the consent ledger gated exactly one purpose (`marketing`) on
-- exactly one channel (outbound email). HxSync is the platform's other way
-- personal data leaves the building: case rows pushed to BigQuery, Snowflake,
-- Kafka or DuckDB. It already redacts fields, but redaction answers "which
-- columns may leave", not "may this person's row leave at all".
--
-- Opt-in per destination, and OFF by default. Turning it on for an existing
-- destination changes what that destination receives, so it must be a
-- deliberate act rather than something a migration does to a running pipeline.
-- A destination with the gate off behaves exactly as it did before this
-- migration.
--
-- Deliberately on `sync_destinations`, not `sync_field_mappings`: the subject
-- of a case row is `case_instances.portal_submitter_email`, which is a
-- property of the case, not of any mapped column. A per-mapping flag would
-- have implied the subject was in the row, and it is not.

ALTER TABLE sync_destinations
    ADD COLUMN IF NOT EXISTS consent_gated BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN sync_destinations.consent_gated IS
    'When true, a case row is pushed only if its portal submitter has a lawful '
    'basis for data_sharing in the consent ledger. Cases with no identified '
    'subject are not gated — there is no one whose consent could be required.';
