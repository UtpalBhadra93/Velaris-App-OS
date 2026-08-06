-- Accelerator Packs — close the check-then-insert race on provisioning.
--
-- `packs.already_provisioned()` reads, then `provision()` inserts. Two
-- concurrent requests for the same (tenant, pack) — a double-clicked button is
-- enough — both pass the read and both insert. The result is two provision rows
-- and two `apply_bundle` runs, which breaks the "provisioned once" guarantee
-- that exists precisely to stop tenant edits being overwritten.
--
-- The rule wanted is "one row with status='provisioned'", which is a partial
-- unique index and therefore not portable to MySQL/MariaDB. So the rule becomes
-- "one row per (tenant, package)" — enforceable everywhere — and a retry after a
-- FAILED provision UPDATES that row instead of inserting a second one. Same
-- behaviour, now guaranteed by the database rather than by timing.
--
-- Any pre-existing duplicates are collapsed first, keeping the successful row
-- (or the newest if none succeeded) so the constraint can be created.
DELETE FROM marketplace_pack_installs a
      USING marketplace_pack_installs b
      WHERE a.tenant_id = b.tenant_id
        AND a.package_id = b.package_id
        AND a.id <> b.id
        AND (
              (b.status = 'provisioned' AND a.status <> 'provisioned')
           OR (b.status = a.status AND b.provisioned_at > a.provisioned_at)
        );

CREATE UNIQUE INDEX IF NOT EXISTS ux_pack_installs_tenant_pkg
    ON marketplace_pack_installs (tenant_id, package_id);
