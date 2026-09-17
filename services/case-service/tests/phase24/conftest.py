"""Guard: the documents router's service singleton must survive this package.

`case_service.api.routers.documents.service` is created once at import and
shared by every request for the life of the process. Several tests here point it
at a `LocalFSBackend` under their own `tmp_path`, which is the right thing to
do — and until 2026-09-08 they did it with a bare assignment that never put it
back. pytest then deleted the `tmp_path`, and every
`GET /documents/{id}/download` for the remainder of the session read from a
directory that no longer existed.

That is a nasty failure to own: it does not fail here, it fails ~700 tests later
in an unrelated package, as one `ObjectNotFound` for a file that is demonstrably
on disk — and in between it silently took the REAL storage path out of every
download test in the run. So the restoration is asserted rather than assumed:
`monkeypatch` gives it, and this notices the moment somebody stops using it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _documents_service_singleton_is_restored():
    from case_service.api.routers import documents as docs_router_mod

    before = docs_router_mod.service
    yield
    assert docs_router_mod.service is before, (
        "case_service.api.routers.documents.service was replaced and not put "
        "back. Use `monkeypatch.setattr(docs_router_mod, 'service', ...)` — a "
        "bare assignment outlives the test and points every later download at "
        "a tmp_path pytest has already deleted."
    )
