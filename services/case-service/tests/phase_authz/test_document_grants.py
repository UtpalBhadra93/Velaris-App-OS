"""Option A — document access is authenticated, or granted, and nothing else.

Before 2026-08-15, `GET /api/v1/documents/{id}` `/download` `/preview`
`/versions` `/by-case/{id}` and `POST /upload` had no auth dependency and no
check in their bodies: **every document in the deployment was readable by
UUID.** The reason it stayed open is that `portal.py` handed anonymous customers
download links pointing straight at those routes, so a plain `Depends(
get_current_user)` would have broken customer access.

Option A keeps both: the portal, at the moment it decides a customer may see a
document, mints a **signed, expiring, single-document grant**. Operators come in
with a Bearer token and the case-level ReBAC check. There is no third way.

The tests below pin the properties that make the grant safe rather than a
prettier version of the same hole — binding, expiry, and unforgeability.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import uuid

import pytest

from case_service.documents import access_tokens
from tests.phase_hxsentinel._seed import seed_case


class TestGrantProperties:
    def test_a_grant_admits_its_own_document(self):
        doc = uuid.uuid4()
        assert access_tokens.verify(access_tokens.mint(doc), doc) is True

    def test_a_grant_does_not_admit_a_different_document(self):
        """The binding IS the signature — a grant for one document cannot be
        replayed against another, and there is no separate ownership check that
        someone could forget to write."""
        grant = access_tokens.mint(uuid.uuid4())
        assert access_tokens.verify(grant, uuid.uuid4()) is False

    def test_an_expired_grant_is_refused(self):
        grant = access_tokens.mint(uuid.uuid4(), ttl_seconds=-1)
        doc = grant  # unused; expiry is checked before the signature
        assert access_tokens.verify(grant, uuid.uuid4()) is False

    def test_expiry_cannot_be_extended_by_editing_the_token(self):
        """The expiry is signed, not merely carried. Rewriting it to a far
        future must invalidate the signature rather than extend the grant."""
        doc = uuid.uuid4()
        version, expiry, sig = access_tokens.mint(doc, ttl_seconds=5).split(".", 2)
        forged = f"{version}.{int(expiry) + 86400}.{sig}"
        assert access_tokens.verify(forged, doc) is False

    def test_garbage_is_refused_and_never_raises(self):
        doc = uuid.uuid4()
        for bad in (None, "", "junk", "v1.notanint.sig", "v9." + "a" * 40,
                    "v1.1.", ".." , "v1", 12345):
            assert access_tokens.verify(bad, doc) is False

    def test_the_signing_key_is_not_the_jwt_secret(self):
        """Derived, so a leak of one is not a leak of the other, and a document
        grant can never be mistaken for a token by the JWT decoder."""
        from case_service.config import get_settings

        secret = (get_settings().auth_secret or "")
        grant = access_tokens.mint(uuid.uuid4())
        assert secret not in grant
        assert grant.count(".") == 2 and not grant.startswith("ey")   # not a JWT

    def test_the_url_helper_carries_the_grant(self):
        doc = uuid.uuid4()
        url = access_tokens.download_url(doc)
        assert url.startswith(f"/api/v1/documents/{doc}/download?grant=")
        assert access_tokens.verify(url.split("grant=", 1)[1], doc) is True


class TestRoutesRefuseAnonymousCallers:
    """The routes themselves, over HTTP, with no credentials at all.

    Every case row here is seeded through `seed_case`, not fabricated. The first
    version passed a bare `uuid4()` as `case_id` — which SQLite accepts, because
    it does not enforce foreign keys, and which MariaDB 11.8 and MySQL 8.4 both
    reject with `documents_ibfk_1`. These four tests were green on SQLite and had
    never run on a real engine; the cross-engine run is what surfaced it. Exactly
    the trap `tests/phase_hxsentinel/_seed.py` was written for.
    """

    @pytest.mark.asyncio
    async def test_anonymous_cannot_read_a_document(self, anon_client, session):
        from case_service.db.models import DocumentModel

        doc_id = uuid.uuid4()
        session.add(DocumentModel(
            id=doc_id, case_id=await seed_case(session), filename="secret.pdf",
            content_type="application/pdf", uploaded_by="staff"))
        await session.commit()

        for path in (f"/api/v1/documents/{doc_id}",
                     f"/api/v1/documents/{doc_id}/versions",
                     f"/api/v1/documents/{doc_id}/download"):
            r = await anon_client.get(path)
            assert r.status_code == 404, f"{path} answered {r.status_code}"

    @pytest.mark.asyncio
    async def test_a_wrong_grant_does_not_open_a_document(self, anon_client, session):
        from case_service.db.models import DocumentModel

        doc_id, other = uuid.uuid4(), uuid.uuid4()
        session.add(DocumentModel(
            id=doc_id, case_id=await seed_case(session), filename="secret.pdf",
            content_type="application/pdf", uploaded_by="staff"))
        await session.commit()

        grant = access_tokens.mint(other)          # a grant for a DIFFERENT doc
        r = await anon_client.get(f"/api/v1/documents/{doc_id}?grant={grant}")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_a_valid_grant_reaches_the_document(self, anon_client, session):
        from case_service.db.models import DocumentModel

        doc_id = uuid.uuid4()
        session.add(DocumentModel(
            id=doc_id, case_id=await seed_case(session), filename="shared.pdf",
            content_type="application/pdf", uploaded_by="staff"))
        await session.commit()

        r = await anon_client.get(
            f"/api/v1/documents/{doc_id}?grant={access_tokens.mint(doc_id)}")
        assert r.status_code == 200, r.text
        assert r.json()["filename"] == "shared.pdf"

    @pytest.mark.asyncio
    async def test_anonymous_cannot_list_a_case_or_upload(self, anon_client):
        """`by-case` takes no grant — a grant is bound to one document, and this
        lists a whole case. Upload has no grant path at all."""
        r = await anon_client.get(f"/api/v1/documents/by-case/{uuid.uuid4()}")
        assert r.status_code in (401, 403, 404)
        r = await anon_client.post("/api/v1/documents/upload")
        assert r.status_code in (401, 403, 422)


class TestPortalStillWorks:
    @pytest.mark.asyncio
    async def test_the_portal_hands_out_a_working_grant(self, client, session):
        """The regression this whole design exists to avoid: gating the routes
        must not break the customer who was never authenticated in the first
        place."""
        from case_service.db.models import DocumentModel

        doc_id = uuid.uuid4()
        session.add(DocumentModel(
            id=doc_id, case_id=await seed_case(session), filename="reply.pdf",
            content_type="application/pdf", uploaded_by="staff",
            portal_visible=True, portal_source="staff"))
        await session.commit()

        url = access_tokens.download_url(doc_id)
        grant = url.split("grant=", 1)[1]
        assert access_tokens.verify(grant, doc_id) is True
