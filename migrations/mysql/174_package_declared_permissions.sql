-- Marketplace manifest permissions become a CEILING (roadmap #29).
--
-- `_permissions` has been resolved at install since manifest v2 shipped —
-- validated strictly, rejecting relations the PDP could never evaluate — and
-- then thrown away. Nothing persisted it and nothing read it, so a package's
-- own declaration of what it may touch was documentation rather than a
-- boundary: it could declare `viewer` on `case` and still reach whatever the
-- installing operator's capability grant allowed.
--
-- These columns carry the declaration from the verified manifest through
-- workspace approval to the install row, where the broker — the only data path
-- a package container has — can enforce it.
--
-- It only ever NARROWS. The operator's grant still decides what is on offer;
-- the manifest decides what the package is allowed to accept. Both must permit
-- a call.
--
-- NULL is meaningful and is the default: "installed before this existed".
-- Those installs keep their current behaviour and are enforced by the grant
-- alone, because an upgrade must not silently revoke access from packages that
-- are working today. The value is captured the next time the package is
-- verified.
-- MySQL has no `ADD COLUMN IF NOT EXISTS` (MariaDB does), so these run once —
-- start-velaris.sh records applied files in schema_migrations and never repeats
-- one, which is what makes the plain form safe here.
ALTER TABLE marketplace_workspace_items
    ADD COLUMN declared_permissions JSON NULL;

ALTER TABLE marketplace_installs
    ADD COLUMN declared_permissions JSON NULL;
