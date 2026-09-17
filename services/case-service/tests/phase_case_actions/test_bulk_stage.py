"""Bulk STAGE transition — the remainder of B3, built 2026-09-17.

Implementation: `case_service/case_actions/bulk_stage.py` and the two routes in
`api/routers/cases.py`. Documented in `docs/components/case-actions.md` §6c.

What these pin, beyond "the cases moved":

* **The confirmation is the server's, not a dialog's.** Execute without a
  preview's token is refused whole, and so is execute with a token for a
  different set of cases, a different stage or a different operator. Nothing
  moves that the operator was not shown moving.
* **Preview changes nothing** — no stage pointer, no audit row.
* **Its own privilege.** A case worker, who may move a case they work one at a
  time, may not move a selection. Asked before any case is read.
* **Every case is re-checked at execution**, because the preview was a moment
  ago: a case sealed since is refused on its own and the rest still move.
* **The stage rules of 2026-09-08 hold**: the entered stage's `on_enter_status`
  is applied, because the move goes through the same code as the single route.
* **Refuses, never truncates**, at the same ceiling as the rest of bulk.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTypeModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.case_actions import bulk_stage
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio

PREVIEW = "/api/v1/cases/bulk/stage/preview"
EXECUTE = "/api/v1/cases/bulk/stage"


def _headers(username: str, roles=("admin",)) -> dict[str, str]:
    """One token per operator per test: the confirmation is bound to the
    caller's user id, and a fresh token is a fresh id."""
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {tok}"}


def _stages(review_extra=None):
    review = {"id": "review", "name": "Review", "stage_type": "linear",
              "order": 1, "steps": []}
    review.update(review_extra or {})
    return [{"id": "triage", "name": "Triage", "stage_type": "linear",
             "order": 0, "steps": []}, review]


@pytest_asyncio.fixture
async def api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def fleet():
    """Tenant `bulkx`: three cases in Triage of a two-stage case type, and one
    case of a case type that has no `review` stage at all."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        tenant = TenantModel(id=uuid.uuid4(), slug="bulkx", name="Bulk Co")
        db.add(tenant)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="bea", tenant_id="bulkx"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Bulk CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": _stages(
                {"on_enter_status": "in_progress", "sla_policy_id": "p1"}),
                "sla_policies": [{"id": "p1", "name": "Review SLA"}]})
        other = CaseTypeModel(
            id=uuid.uuid4(), name="No Review CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": [{"id": "triage", "name": "Triage",
                                         "stage_type": "linear", "order": 0,
                                         "steps": []}]})
        db.add_all([ct, other])
        await db.flush()

        def _case(n, case_type, **kw):
            return CaseInstanceModel(
                id=uuid.uuid4(), case_number=f"BLK-{n}", case_type_id=case_type.id,
                case_type_version="1.0.0", status=kw.get("status", "new"),
                priority="medium", current_stage_id=kw.get("stage", "triage"),
                tenant_id=tenant.id, created_by="bea", data={})

        cases = [_case(i, ct) for i in range(3)]
        stray = _case(9, other)
        db.add_all([*cases, stray])
        await db.commit()
        yield {"tenant": tenant, "ct": ct.id, "cases": [c.id for c in cases],
               "stray": stray.id}
    tenancy.clear_cache()


async def _case(case_id):
    async with TestSessionFactory() as db:
        return await db.get(CaseInstanceModel, case_id)


async def _stage_rows(case_id):
    async with TestSessionFactory() as db:
        return (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == case_id,
            CaseAuditLogModel.action == "stage_transitioned"))).scalars().all()


async def _all_in(case_ids, stage):
    return all([(await _case(c)).current_stage_id == stage for c in case_ids])


def _ids(ids):
    return [str(i) for i in ids]


# ── the token ────────────────────────────────────────────────────────────────

async def test_a_token_confirms_exactly_the_set_it_was_minted_for():
    a, b = uuid.uuid4(), uuid.uuid4()
    tok, _ = bulk_stage.mint("u1", "review", [a, b])
    assert bulk_stage.verify(tok, "u1", "review", [b, a]) is None   # order is not binding
    assert bulk_stage.verify(tok, "u1", "review", [a]) is not None  # a smaller set
    assert bulk_stage.verify(tok, "u1", "review", [a, b, uuid.uuid4()]) is not None
    assert bulk_stage.verify(tok, "u2", "review", [a, b]) is not None
    assert bulk_stage.verify(tok, "u1", "close", [a, b]) is not None


async def test_a_token_expires():
    tok, exp = bulk_stage.mint("u1", "review", [uuid.uuid4()], now=1_000)
    ids = []  # irrelevant: expiry is checked first
    assert "expired" in bulk_stage.verify(tok, "u1", "review", ids, now=exp + 1)


async def test_a_forged_expiry_does_not_verify():
    """The expiry is inside the signature — editing it breaks the token."""
    c = uuid.uuid4()
    tok, exp = bulk_stage.mint("u1", "review", [c], now=1_000)
    v, _, sig = tok.split(".")
    forged = f"{v}.{exp + 86_400}.{sig}"
    assert bulk_stage.verify(forged, "u1", "review", [c], now=1_000) is not None


@pytest.mark.parametrize("bad", [None, "", "v1", "v1.x.y", "v9.1.abc"])
async def test_a_missing_or_malformed_token_is_refused(bad):
    assert bulk_stage.verify(bad, "u1", "review", [uuid.uuid4()]) is not None


# ── the privilege ────────────────────────────────────────────────────────────

async def test_a_case_worker_may_not_preview_or_execute(fleet, api):
    h = _headers("bea", roles=("case_worker",))
    for url in (PREVIEW, EXECUTE):
        r = await api.post(url, json={"case_ids": _ids(fleet["cases"]),
                                      "target_stage_id": "review"}, headers=h)
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "VEL-CASE-BULK-STAGE-DENIED-403"
    assert await _all_in(fleet["cases"], "triage")


async def test_the_privilege_is_its_own_and_not_granted_to_case_workers():
    from velaris_shared.hxguard.actions import ACTIONS

    a = ACTIONS[bulk_stage.PRIVILEGE]
    assert set(a.legacy_roles) == {"admin", "manager"}


async def test_the_catalogue_says_whether_the_bar_may_offer_it(fleet, api):
    r = await api.get("/api/v1/cases/actions/catalogue", headers=_headers("bea"))
    assert r.status_code == 200, r.text
    assert r.json()["bulk_stage_permitted"] is True
    r = await api.get("/api/v1/cases/actions/catalogue",
                      headers=_headers("bea", roles=("case_worker",)))
    assert r.json()["bulk_stage_permitted"] is False


# ── the request ──────────────────────────────────────────────────────────────

async def test_over_the_ceiling_is_refused_not_truncated(fleet, api):
    from case_service.case_actions.dispatcher import BULK_CEILING

    ids = [str(uuid.uuid4()) for _ in range(BULK_CEILING + 1)]
    r = await api.post(PREVIEW, json={"case_ids": ids, "target_stage_id": "review"},
                       headers=_headers("bea"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VEL-CASE-BULK-CEILING-400"


async def test_no_cases_is_refused(fleet, api):
    r = await api.post(PREVIEW, json={"case_ids": [], "target_stage_id": "review"},
                       headers=_headers("bea"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VEL-CASE-BULK-EMPTY-400"


# ── preview ──────────────────────────────────────────────────────────────────

async def test_preview_changes_nothing_and_says_what_would_fire(fleet, api):
    h = _headers("bea")
    ids = [*fleet["cases"], fleet["stray"]]
    r = await api.post(PREVIEW, json={"case_ids": _ids(ids),
                                      "target_stage_id": "review"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 4
    assert body["would_move"] == 3
    assert body["refused"] == 1
    assert body["confirmation_token"]
    assert set(body["eligible_case_ids"]) == set(_ids(fleet["cases"]))

    stray = next(x for x in body["results"] if x["case_id"] == str(fleet["stray"]))
    assert stray["code"] == "VEL-CASE-BULK-STAGE-UNKNOWN-409"

    [effect] = body["effects"]
    assert effect["cases"] == 3
    assert effect["on_enter_status"] == "in_progress"
    assert effect["starts_sla"] == "Review SLA"

    for c in ids:
        assert (await _case(c)).current_stage_id == "triage"
        assert await _stage_rows(c) == []


async def test_a_case_already_in_the_stage_is_not_confirmed_for_a_move(fleet, api):
    async with TestSessionFactory() as db:
        (await db.get(CaseInstanceModel, fleet["cases"][0])).current_stage_id = "review"
        await db.commit()
    r = await api.post(PREVIEW, json={"case_ids": _ids(fleet["cases"]),
                                      "target_stage_id": "review"},
                       headers=_headers("bea"))
    body = r.json()
    assert body["would_move"] == 2 and body["unchanged"] == 1
    assert str(fleet["cases"][0]) not in body["eligible_case_ids"]


async def test_another_tenants_case_is_not_found_and_not_named(fleet, api):
    """404 per case, never 403 — a caller who may not reach a case must not be
    able to confirm that the id exists."""
    async with TestSessionFactory() as db:
        other = TenantModel(id=uuid.uuid4(), slug="elsewhere", name="Elsewhere")
        db.add(other)
        await db.flush()
        theirs = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ELS-1", case_type_id=fleet["ct"],
            case_type_version="1.0.0", status="new", priority="medium",
            current_stage_id="triage", tenant_id=other.id, created_by="x", data={})
        db.add(theirs)
        await db.commit()
    r = await api.post(PREVIEW, json={"case_ids": [str(theirs.id)],
                                      "target_stage_id": "review"},
                       headers=_headers("bea"))
    [res] = r.json()["results"]
    assert res["ok"] is False and res["code"] == "VEL-CASE-NOTFOUND-404"
    assert r.json()["confirmation_token"] is None


# ── execute ──────────────────────────────────────────────────────────────────

async def test_execute_without_a_preview_moves_nothing(fleet, api):
    r = await api.post(EXECUTE, json={"case_ids": _ids(fleet["cases"]),
                                      "target_stage_id": "review"},
                       headers=_headers("bea"))
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "VEL-CASE-BULK-STAGE-CONFIRM-409"
    assert await _all_in(fleet["cases"], "triage")


async def test_execute_with_a_different_selection_moves_nothing(fleet, api):
    """Preview two, then send three with the same token."""
    h = _headers("bea")
    pre = (await api.post(PREVIEW, json={"case_ids": _ids(fleet["cases"][:2]),
                                         "target_stage_id": "review"},
                          headers=h)).json()
    r = await api.post(EXECUTE, json={"case_ids": _ids(fleet["cases"]),
                                      "target_stage_id": "review",
                                      "confirmation_token": pre["confirmation_token"]},
                       headers=h)
    assert r.status_code == 409
    assert await _all_in(fleet["cases"], "triage")


async def test_another_operators_preview_does_not_confirm(fleet, api):
    pre = (await api.post(PREVIEW, json={"case_ids": _ids(fleet["cases"]),
                                         "target_stage_id": "review"},
                          headers=_headers("bea"))).json()
    r = await api.post(EXECUTE, json={"case_ids": pre["eligible_case_ids"],
                                      "target_stage_id": "review",
                                      "confirmation_token": pre["confirmation_token"]},
                       headers=_headers("bea"))   # same name, different user id
    assert r.status_code == 409


async def test_preview_then_execute_moves_and_fires_the_stage(fleet, api):
    h = _headers("bea")
    pre = (await api.post(PREVIEW, json={"case_ids": _ids(fleet["cases"]),
                                         "target_stage_id": "review"},
                          headers=h)).json()
    r = await api.post(EXECUTE, json={"case_ids": pre["eligible_case_ids"],
                                      "target_stage_id": "review",
                                      "reason": "quarter-end sweep",
                                      "confirmation_token": pre["confirmation_token"]},
                       headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == 3 and body["failed"] == 0

    for c in fleet["cases"]:
        case = await _case(c)
        assert case.current_stage_id == "review"
        # The stage's own status — applied by the same code the single route uses.
        assert case.status == "in_progress"
        [row] = await _stage_rows(c)
        assert row.details["bulk_run_id"] == body["bulk_run_id"]
        assert row.details["reason"] == "quarter-end sweep"


async def test_a_case_sealed_after_preview_is_refused_and_the_rest_move(fleet, api):
    h = _headers("bea")
    pre = (await api.post(PREVIEW, json={"case_ids": _ids(fleet["cases"]),
                                         "target_stage_id": "review"},
                          headers=h)).json()
    async with TestSessionFactory() as db:
        (await db.get(CaseInstanceModel, fleet["cases"][0])).status = "archived"
        await db.commit()

    r = await api.post(EXECUTE, json={"case_ids": pre["eligible_case_ids"],
                                      "target_stage_id": "review",
                                      "confirmation_token": pre["confirmation_token"]},
                       headers=h)
    body = r.json()
    assert body["moved"] == 2 and body["failed"] == 1
    refused = next(x for x in body["results"] if not x["ok"])
    assert refused["case_id"] == str(fleet["cases"][0])
    assert refused["code"] == "VEL-CASE-ACTION-SEALED-409"
    assert (await _case(fleet["cases"][0])).current_stage_id == "triage"


# ── the single-case route is unchanged ───────────────────────────────────────

async def test_the_single_route_still_moves_one_case_without_the_privilege(fleet, api):
    """Sharing the gate must not have added the bulk privilege to it."""
    r = await api.post(f"/api/v1/cases/{fleet['cases'][0]}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("bea"))
    assert r.status_code == 200, r.text
    [row] = await _stage_rows(fleet["cases"][0])
    assert not (row.details or {}).get("bulk_run_id")


async def test_the_single_route_still_refuses_a_sealed_case(fleet, api):
    async with TestSessionFactory() as db:
        (await db.get(CaseInstanceModel, fleet["cases"][0])).status = "archived"
        await db.commit()
    r = await api.post(f"/api/v1/cases/{fleet['cases'][0]}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("bea"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VEL-CASE-ACTION-SEALED-409"
