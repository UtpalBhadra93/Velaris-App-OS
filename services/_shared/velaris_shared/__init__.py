"""velaris_shared — the layer every Velaris service depends on.

Carved out of `case_service` at roadmap item 13b so that services extracted in
13c–e (analytics/process-mining, HxGraph-sync, HxMigrate) share ONE definition
of the database, the identity model and the authorization decision point rather
than duplicating them or calling back into case-service for models.

Contents, and the rule that keeps this liftable:

* `config`  — settings (`VELARIS_CASE_*`).
* `db`      — SQLAlchemy models, session/pool factories, repository, backends.
* `auth`    — JWT, devices, WebAuthn, the FastAPI identity dependencies.
* `hxguard` — the in-process policy decision point and its action catalogue.

**Nothing here may import a feature module.** `tests/phase_arch/
test_import_boundaries.py` enforces it; that hard rule is what made this move a
mechanical one instead of a rewrite.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""

__all__ = ["auth", "config", "db", "hxguard"]
