"""The tenant boundary on every CASE-SCOPED surface (audit of 2026-09-06).

2026-09-05 closed `GET /cases/{id}`, which had no tenant clause at all. This
file is what the sweep after it found: the same hole on the surfaces that hang
off a case rather than being one.

`hxguard.require_case` answers *"does this operator have a relationship to this
case"*. It never had an opinion about tenants, so **nineteen route handlers that
gated on it alone were readable across the boundary** — and two more had no case
gate at all. Proven live, on a running box, before any of it was written:

* tenant `dev` listed and then **downloaded the file content** of a document on
  a case belonging to tenant `default`;
* tenant `dev` asked **HxNexus** to summarise the same case and was told its
  status, its priority and the contents of a note marked confidential;
* tenant `dev` **published** that document to the customer portal.

And one that failed the other way: costing compared a `GUID` column to a slug,
so every tenant-owned case answered "Case not found" **to its own tenant**.

The tests mint their own tokens. The suite's default `client` is
**admin + superadmin**, and superadmin is the documented break-glass across
tenants — a boundary test written with it would pass no matter what the code
did.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio


def _token(username: str, roles=("admin",)) -> str:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")


def _headers(username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(username)}"}


@pytest_asyncio.fixture
async def two_tenants():
    """Two tenants, one operator in each, and a case owned by the first."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        beta = TenantModel(id=uuid.uuid4(), slug="beta", name="Beta")
        db.add_all([acme, beta])
        db.add_all([
            UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"),
            UserDirectoryModel(id=uuid.uuid4(), user_id="ben", tenant_id="beta"),
        ])
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Boundary CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ACME-1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="high",
            tenant_id=acme.id, created_by="ann",
            data={"secret_note": "acme's confidential note"})
        untenanted = CaseInstanceModel(
            id=uuid.uuid4(), case_number="SHARED-1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="low",
            tenant_id=None, created_by="ann", data={})
        db.add_all([case, untenanted])
        await db.commit()
        yield {"acme": acme, "beta": beta, "case": case.id, "shared": untenanted.id}
    tenancy.clear_cache()


@pytest_asyncio.fixture
async def api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── the case-scoped gate itself ─────────────────────────────────────────────

async def test_require_case_refuses_another_tenants_case(session, two_tenants):
    """The fix at its source, so it holds for every caller of `require_case`
    rather than for the nineteen routes that happened to be audited."""
    from velaris_shared import hxguard
    from velaris_shared.auth.models import AuthenticatedUser
    from fastapi import HTTPException

    ben = AuthenticatedUser(token="x", user_id="ben", username="ben", email="",
                            roles=["admin"], groups=[], department="")
    ben.tenant_id = "beta"
    with pytest.raises(HTTPException) as exc:
        await hxguard.require_case(session, ben, "case.read", two_tenants["case"])
    # 404, never 403: a caller who may not reach a case must not be able to
    # confirm that the id exists.
    assert exc.value.status_code == 404


async def test_require_case_allows_the_owning_tenant(session, two_tenants):
    from velaris_shared import hxguard
    from velaris_shared.auth.models import AuthenticatedUser

    ann = AuthenticatedUser(token="x", user_id="ann", username="ann", email="",
                            roles=["admin"], groups=[], department="")
    ann.tenant_id = "acme"
    await hxguard.require_case(session, ann, "case.read", two_tenants["case"])


async def test_an_untenanted_case_stays_reachable_by_everyone(session, two_tenants):
    """The compatibility claim, and it is the whole reason this is safe to ship:
    every release so far has shown a NULL-tenant case to everybody, and several
    single-tenant installs have nothing but NULL-tenant cases. Only
    `VELARIS_CASE_TENANT_STRICT_READS` closes that, and only after migration 197
    reports zero rows left."""
    from velaris_shared import hxguard
    from velaris_shared.auth.models import AuthenticatedUser

    ben = AuthenticatedUser(token="x", user_id="ben", username="ben", email="",
                            roles=["admin"], groups=[], department="")
    ben.tenant_id = "beta"
    await hxguard.require_case(session, ben, "case.read", two_tenants["shared"])


async def test_superadmin_keeps_its_break_glass(session, two_tenants):
    from velaris_shared import hxguard
    from velaris_shared.auth.models import AuthenticatedUser

    root = AuthenticatedUser(token="x", user_id="root", username="root", email="",
                             roles=["admin", "superadmin"], groups=[], department="")
    root.tenant_id = "beta"
    await hxguard.require_case(session, root, "case.read", two_tenants["case"])


# ── the surfaces that were proven open ──────────────────────────────────────

async def test_documents_on_another_tenants_case_are_absent(api, two_tenants):
    """`GET /documents/by-case/{id}` — the one that was proven live. The list is
    the disclosure; the download that follows it is the damage."""
    r = await api.get(f"/api/v1/documents/by-case/{two_tenants['case']}",
                      headers=_headers("ben"))
    assert r.status_code == 404, r.text


async def test_capture_digest_on_another_tenants_case_is_absent(api, two_tenants):
    r = await api.get(f"/api/v1/capture/cases/{two_tenants['case']}",
                      headers=_headers("ben"))
    assert r.status_code == 404, r.text


async def test_case_event_stream_of_another_tenant_is_absent(api, two_tenants):
    r = await api.get(f"/api/v1/cases/{two_tenants['case']}/events",
                      headers=_headers("ben"))
    assert r.status_code == 404, r.text


async def test_ai_action_proposals_of_another_tenant_are_absent(api, two_tenants):
    r = await api.get(f"/api/v1/cases/{two_tenants['case']}/ai-actions",
                      headers=_headers("ben"))
    assert r.status_code == 404, r.text


async def test_hxnexus_will_not_answer_about_another_tenants_case(api, two_tenants):
    """HxNexus had its own case gate — `_assert_case_access` — whose comment read
    "admins and managers have platform-wide case access". That was written when
    the platform had one tenant; on a shared install `admin` is a TENANT admin.

    Worse than reading the row: an answer is not bounded by the fields a screen
    would have shown, and the proof run had the model volunteer the contents of
    a note marked confidential.
    """
    r = await api.post(f"/api/v1/hxnexus/cases/{two_tenants['case']}/qa",
                       headers=_headers("ben"), json={"question": "what is this?"})
    assert r.status_code == 404, r.text


async def test_portal_visibility_of_another_tenants_document_is_absent(
        api, session, two_tenants):
    """`PATCH /documents/{id}/portal-visibility` had NO authorization beyond a
    valid token — and the flag it sets is the one that decides whether somebody
    outside the platform can fetch the file. Proven live on 2026-09-06 before
    the fix: `beta` published `acme`'s document.

    A real document, not a random id: a 404 on an id that never existed proves
    nothing at all about the gate.
    """
    from velaris_shared.db.models import DocumentModel

    doc = DocumentModel(id=uuid.uuid4(), case_id=two_tenants["case"],
                        filename="acme-private.txt", content_type="text/plain",
                        current_version=1)
    session.add(doc)
    await session.commit()

    r = await api.patch(f"/api/v1/documents/{doc.id}/portal-visibility",
                        headers=_headers("ben"), json={"portal_visible": True})
    assert r.status_code == 404, r.text
    await session.refresh(doc)
    assert doc.portal_visible is not True, "the flag was flipped anyway"

    # …and the owning tenant can still do it.
    r = await api.patch(f"/api/v1/documents/{doc.id}/portal-visibility",
                        headers=_headers("ann"), json={"portal_visible": True})
    assert r.status_code == 200, r.text


async def test_deleting_another_tenants_document_is_absent(
        api, session, two_tenants):
    """`DELETE /documents/{id}` asked only "admin, or the case's creator?", and
    `docs.files.delete` is a PLATFORM privilege — so a tenant admin could delete
    another tenant's evidence."""
    from velaris_shared.db.models import DocumentModel

    doc = DocumentModel(id=uuid.uuid4(), case_id=two_tenants["case"],
                        filename="acme-evidence.txt", content_type="text/plain",
                        current_version=1)
    session.add(doc)
    await session.commit()

    r = await api.delete(f"/api/v1/documents/{doc.id}", headers=_headers("ben"))
    assert r.status_code == 404, r.text
    await session.refresh(doc)
    assert doc.is_deleted is not True, "it was deleted anyway"


# ── the one that failed closed ──────────────────────────────────────────────

async def test_costing_reaches_a_case_in_its_own_tenant(api, two_tenants):
    """`case.tenant_id` is a GUID and costing compared it to a SLUG, so the
    compare was never equal and time tracking answered "Case not found" to the
    case's OWN tenant. Never an exposure — simply unavailable, silently, on
    every tenanted case."""
    r = await api.get(f"/api/v1/costing/cases/{two_tenants['case']}/time",
                      headers=_headers("ann"))
    assert r.status_code == 200, r.text


async def test_costing_still_refuses_another_tenants_case(api, two_tenants):
    r = await api.get(f"/api/v1/costing/cases/{two_tenants['case']}/time",
                      headers=_headers("ben"))
    assert r.status_code == 404, r.text
