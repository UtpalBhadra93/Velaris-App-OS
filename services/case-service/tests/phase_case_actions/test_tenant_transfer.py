"""Moving a case between tenants — built 2026-09-17, migration 205.

Implementation: `case_service/case_actions/tenant_transfer.py`.
Design: `docs/Future/Done/CASE-TENANT-TRANSFER-DESIGN.md`.

What these pin, in order of how badly each would hurt:

* **Every case-linked table is classified.** A new table that carries a case id
  fails this suite until somebody decides whether it moves, stays or blocks.
* **Never partially move.** A failure part way leaves the case, its documents and
  its links exactly where they were.
* **Three people, two tenants.** The requester cannot approve their own request;
  the destination sees nothing until the source's second person approved, and
  only the destination can accept.
* **What blocks, blocks** — money, evidence, holds, a case type the destination
  cannot run — before any write, with the reason named.
* **What moves is re-tagged in its own spelling**, a slug stays a slug; what is
  closed is closed; what is severed is gone and in the manifest; the history
  arrives with pseudonyms, not names.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update

from velaris_shared.db.models import (
    Base,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTenantTransferModel,
    CaseTypeModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.case_actions import tenant_transfer as tt
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio

T = Base.metadata.tables
NOW = datetime.now(timezone.utc)


def _headers(username: str, roles=("admin",)) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(user_id=str(uuid.uuid4()), username=username, roles=list(roles),
                           secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """Source `srcco` (alice, bob), destination `dstco` (dave), stranger `othco`
    (olga). One case in srcco on a platform-shared case type, with a bit of
    everything hanging off it."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        src = TenantModel(id=uuid.uuid4(), slug="srcco", name="Source Co")
        dst = TenantModel(id=uuid.uuid4(), slug="dstco", name="Destination Co")
        oth = TenantModel(id=uuid.uuid4(), slug="othco", name="Other Co")
        db.add_all([src, dst, oth])
        for user, slug in (("alice", "srcco"), ("bob", "srcco"), ("dave", "dstco"),
                           ("olga", "othco")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=user, tenant_id=slug))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Shared CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                           definition_json={"stages": [{"id": "s1", "name": "S1", "steps": []}]})
        db.add(ct)
        await db.flush()

        def case(n, **kw):
            return CaseInstanceModel(
                id=uuid.uuid4(), case_number=f"TT-{n}", case_type_id=ct.id,
                case_type_version="1.0.0", status="open", priority="medium",
                current_stage_id="s1", tenant_id=src.id, created_by="alice", data={},
                portal_submitter_email=kw.get("email"), **{k: v for k, v in kw.items()
                                                          if k != "email"})
        main = case(1, portal_tracking_token=uuid.uuid4())
        other = case(2)
        db.add_all([main, other])
        await db.flush()
        child = case(3, parent_case_id=main.id)
        db.add(child)

        doc_slug, doc_guid = uuid.uuid4(), uuid.uuid4()
        await db.execute(insert(T["documents"]).values(
            id=doc_slug, case_id=main.id, filename="a.pdf", tenant_id="srcco"))
        await db.execute(insert(T["documents"]).values(
            id=doc_guid, case_id=main.id, filename="b.pdf", tenant_id=str(src.id)))
        await db.execute(insert(T["hxnexus_document_chunks"]).values(
            id=uuid.uuid4(), case_id=main.id, chunk_text="hello", tenant_id="srcco"))
        await db.execute(insert(T["case_messages"]).values(
            id=uuid.uuid4(), case_id=main.id, author="alice", body="internal note"))
        await db.execute(insert(T["case_assignments"]).values(
            id=uuid.uuid4(), case_id=main.id, step_id="st", assignee_type="user",
            assignee_id="alice", status="active"))
        await db.execute(insert(T["case_sla_instances"]).values(
            id=uuid.uuid4(), case_id=main.id, sla_policy_id="p", target_id="s1",
            status="on_track", started_at=NOW, goal_at=NOW + timedelta(days=1),
            deadline_at=NOW + timedelta(days=2)))
        await db.execute(insert(T["case_relationships"]).values(
            id=uuid.uuid4(), source_case_id=main.id, target_case_id=other.id,
            relationship_type="related"))
        await db.execute(insert(T["hxguard_tuples"]).values(
            id=uuid.uuid4(), object_type="case", object_id=main.id, relation="editor",
            subject_type="user", subject_id="alice"))
        db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=main.id, action="note_added",
                                 actor_id="alice", actor_type="user",
                                 details={"claimed_by": "alice@src.test", "size": 3}))
        await db.commit()
        yield {"src": src, "dst": dst, "oth": oth, "ct": ct, "case": main.id,
               "other": other.id, "child": child.id, "doc_slug": doc_slug,
               "doc_guid": doc_guid}
    tenancy.clear_cache()


async def _request(api, world, who="alice", headers=None):
    return await api.post(f"/api/v1/cases/{world['case']}/tenant-transfers",
                          json={"destination_tenant": "dstco",
                                "reason": "customer moved to Destination Co"},
                          headers=headers or _headers(who))


async def _approved(api, world):
    r = await _request(api, world)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/approve", headers=_headers("bob"))
    assert r.status_code == 200, r.text
    return tid


async def _case(case_id):
    async with TestSessionFactory() as db:
        return await db.get(CaseInstanceModel, case_id)


# ── the classification ───────────────────────────────────────────────────────

async def test_every_case_linked_table_is_classified():
    """A new table with a case id must take a position before this passes."""
    assert tt.unclassified() == set()


async def test_no_table_is_in_two_classes():
    sets = [set(tt.MOVE_RETAG), set(tt.MOVE_WITH_CASE), set(tt.CLOSE), set(tt.SEVER),
            set(tt.BLOCK), set(tt.STAY)]
    seen: set[str] = set()
    for s in sets:
        assert not (s & seen), s & seen
        seen |= s


async def test_every_retagged_table_has_a_tenant_column():
    for name in tt.MOVE_RETAG:
        assert "tenant_id" in T[name].c and "case_id" in T[name].c, name


# ── the flow ─────────────────────────────────────────────────────────────────

async def test_the_whole_move(api, world):
    tid = await _approved(api, world)

    r = await api.get("/api/v1/case-tenant-transfers?direction=incoming", headers=_headers("dave"))
    [incoming] = r.json()["items"]
    assert incoming["case_number"] == "TT-1"
    assert "requested_by" not in incoming, "the destination is not given source staff ids"

    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept",
                       json={"note": "welcome"}, headers=_headers("dave"))
    assert r.status_code == 200, r.text
    manifest = r.json()["manifest"]
    assert r.json()["state"] == "completed"

    case = await _case(world["case"])
    assert case.tenant_id == world["dst"].id
    assert case.portal_tracking_token is None
    assert case.created_by.startswith("actor:")
    assert (await _case(world["child"])).parent_case_id is None

    async with TestSessionFactory() as db:
        docs = dict((await db.execute(select(T["documents"].c.id, T["documents"].c.tenant_id)
                                      .where(T["documents"].c.case_id == world["case"]))).all())
        assert docs[world["doc_slug"]] == "dstco", "a slug stays a slug"
        assert docs[world["doc_guid"]] == str(world["dst"].id), "a GUID stays a GUID"
        statuses = (await db.execute(select(T["case_assignments"].c.status)
                                     .where(T["case_assignments"].c.case_id == world["case"]))).scalars().all()
        assert statuses == ["cancelled"]
        assert (await db.execute(select(T["case_relationships"]))).all() == []
        assert (await db.execute(select(T["hxguard_tuples"]))).all() == []
        audit = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == world["case"]))).scalars().all()
        assert all((a.actor_id or "").startswith("actor:") for a in audit)
        note = next(a for a in audit if a.action == "note_added")
        assert note.details["claimed_by"] == "[redacted]" and note.details["size"] == 3
        assert any(a.action == "tenant_transferred" for a in audit)

    assert manifest["moved"]["documents"] == 2
    assert manifest["closed"]["case_assignments"] == 1
    assert manifest["severed"]["case_relationships"] == 1
    assert manifest["severed"]["child_cases"] == 1

    # The source keeps the tombstone and loses the case.
    r = await api.get("/api/v1/case-tenant-transfers?direction=outgoing", headers=_headers("alice"))
    assert r.json()["items"][0]["state"] == "completed"
    r = await api.get(f"/api/v1/cases/{world['case']}", headers=_headers("alice"))
    assert r.status_code == 404


async def test_the_requester_cannot_approve_their_own_request(api, world):
    h = _headers("alice")
    r = await _request(api, world, headers=h)
    r = await api.post(f"/api/v1/case-tenant-transfers/{r.json()['id']}/approve", headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "VEL-CASE-TENANT-TRANSFER-TWO-PERSON-403"


async def test_the_destination_sees_nothing_before_approval_and_cannot_act_for_the_source(api, world):
    r = await _request(api, world)
    tid = r.json()["id"]
    r = await api.get("/api/v1/case-tenant-transfers?direction=incoming", headers=_headers("dave"))
    assert r.json()["items"] == []
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/approve", headers=_headers("dave"))
    assert r.status_code == 404, "an unapproved request does not exist for the destination"


async def test_the_source_cannot_accept_for_the_destination(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("bob"))
    assert r.status_code == 403
    assert (await _case(world["case"])).tenant_id == world["src"].id


async def test_a_stranger_tenant_gets_a_404(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("olga"))
    assert r.status_code == 404


async def test_a_refusal_moves_nothing_and_frees_the_case(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/refuse",
                       json={"note": "not ours"}, headers=_headers("dave"))
    assert r.json()["state"] == "refused"
    assert (await _case(world["case"])).tenant_id == world["src"].id
    r = await _request(api, world)
    assert r.status_code == 201, "a refused transfer does not block a new request"


async def test_one_open_transfer_per_case(api, world):
    assert (await _request(api, world)).status_code == 201
    r = await _request(api, world)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VEL-CASE-TENANT-TRANSFER-STATE-409"


async def test_without_the_privilege_nothing(api, world):
    r = await _request(api, world, headers=_headers("alice", roles=("case_worker",)))
    assert r.status_code == 403


# ── what blocks ──────────────────────────────────────────────────────────────

async def test_money_blocks_before_any_write(api, world):
    async with TestSessionFactory() as db:
        await db.execute(insert(T["payment_requests"]).values(
            id=uuid.uuid4(), tenant_id="srcco", case_id=str(world["case"]), step_id="p",
            provider="stripe", amount_cents=100))
        await db.commit()
    r = await _request(api, world)
    assert r.status_code == 409, r.text
    assert "payment requests" in r.text


async def test_a_case_type_the_destination_cannot_run_blocks(api, world):
    async with TestSessionFactory() as db:
        (await db.get(CaseTypeModel, world["ct"].id)).tenant_id = world["src"].id
        await db.commit()
    r = await _request(api, world)
    assert r.status_code == 409 and "case type" in r.text


async def test_an_open_risk_hold_blocks(api, world):
    async with TestSessionFactory() as db:
        await db.execute(insert(T["case_risk_holds"]).values(
            id=uuid.uuid4(), case_id=world["case"], finding_id=uuid.uuid4(),
            rule_key="r", severity="high", status="open"))
        await db.commit()
    r = await _request(api, world)
    assert r.status_code == 409 and "risk hold" in r.text


async def test_a_blocker_that_appears_after_approval_stops_the_accept(api, world):
    tid = await _approved(api, world)
    async with TestSessionFactory() as db:
        await db.execute(insert(T["esign_requests"]).values(
            id=uuid.uuid4(), tenant_id="srcco", case_id=str(world["case"]), step_id="e"))
        await db.commit()
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("dave"))
    assert r.status_code == 409 and "e-signature" in r.text
    assert (await _case(world["case"])).tenant_id == world["src"].id


async def test_a_failure_part_way_leaves_everything_where_it_was(api, world, monkeypatch):
    """The transaction is the guarantee. Fail at the very last write — after
    documents were re-tagged, staff pseudonymised, assignments closed, links
    severed and the case re-tenanted — and prove none of it stuck."""
    tid = await _approved(api, world)

    import case_service.case_actions.tenant_transfer as mod

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mod, "CaseAuditLogModel", boom)
    with pytest.raises(RuntimeError):
        await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("dave"))

    case = await _case(world["case"])
    assert case.tenant_id == world["src"].id
    assert case.created_by == "alice"
    assert case.portal_tracking_token is not None
    async with TestSessionFactory() as db:
        tags = (await db.execute(select(T["documents"].c.tenant_id)
                                 .where(T["documents"].c.case_id == world["case"]))).scalars().all()
        assert sorted(tags) == sorted(["srcco", str(world["src"].id)])
        statuses = (await db.execute(select(T["case_assignments"].c.status))).scalars().all()
        assert statuses == ["active"]
        assert len((await db.execute(select(T["case_relationships"]))).all()) == 1
        authors = (await db.execute(select(T["case_messages"].c.author))).scalars().all()
        assert authors == ["alice"]
        row = await db.get(CaseTenantTransferModel, uuid.UUID(tid))
        assert row.state == "pending"


async def test_every_person_column_that_moves_is_pseudonymised():
    """A moved table that gains a `*_by` / author / actor column must be listed,
    or a source-staff identity travels to another company unnoticed."""
    for name in tt.MOVE_RETAG | tt.MOVE_WITH_CASE:
        named = {c.name for c in T[name].columns if tt.PERSON_COLUMN_PATTERN.search(c.name)}
        assert named <= set(tt.PERSON_COLUMNS.get(name, ())), (name, named)


async def test_staff_identities_do_not_travel(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("dave"))
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        [author] = (await db.execute(select(T["case_messages"].c.author))).scalars().all()
        assert author.startswith("actor:")


async def _seed_comms(world, *, email_on_case="cust@example.com"):
    """The source's WhatsApp endpoint, an open conversation on the case with one
    message each way, an inbound email, an outbound SMS; and the customer known
    to the source as a portal customer with that phone — rung 2's input."""
    from velaris_shared.db.models import (
        ChannelConversationModel, ChannelEndpointModel, ChannelMessageModel, PortalCustomerModel,
    )
    async with TestSessionFactory() as db:
        ep = ChannelEndpointModel(id=uuid.uuid4(), tenant_id="srcco", channel="whatsapp",
                                  provider="twilio", address="+15550001", routing={},
                                  auto_create_case=False)
        db.add(ep)
        await db.flush()
        conv = ChannelConversationModel(id=uuid.uuid4(), tenant_id="srcco", endpoint_id=ep.id,
                                        peer_address="+447700900123", case_id=world["case"],
                                        status="open", assigned_to="alice", metadata_json={},
                                        last_message_at=NOW)
        db.add(conv)
        await db.flush()
        db.add(ChannelMessageModel(tenant_id="srcco", conversation_id=conv.id, case_id=world["case"],
                                   channel="whatsapp", direction="inbound",
                                   peer_address="+447700900123", body="my claim is late",
                                   raw_payload={}, received_at=NOW))
        db.add(ChannelMessageModel(tenant_id="srcco", conversation_id=conv.id, case_id=world["case"],
                                   channel="whatsapp", direction="outbound", author="alice",
                                   peer_address="+447700900123", body="looking into it",
                                   raw_payload={}, sent_at=NOW + timedelta(minutes=1)))
        await db.execute(insert(T["email_messages"]).values(
            id=uuid.uuid4(), case_id=world["case"], direction="inbound",
            message_id="<m1@src>", subject="about my claim", from_address="cust@example.com",
            body_text="please call me", tenant_id="srcco", received_at=NOW))
        await db.execute(insert(T["sms_messages"]).values(
            id=uuid.uuid4(), tenant_id="srcco", case_id=world["case"], step_id="s",
            to_number="+447700900123", body="your claim was received", status="sent"))
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=world["src"].id,
                                   primary_email=email_on_case, display_name="Cust",
                                   phone="+447700900123"))
        (await db.get(CaseInstanceModel, world["case"])).portal_submitter_email = email_on_case
        await db.commit()
        return ep.id, conv.id


async def _move(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/accept", json={},
                       headers=_headers("dave"))
    assert r.status_code == 200, r.text
    return r.json()


async def test_customer_communications_no_longer_block_the_move(api, world):
    await _seed_comms(world)
    r = await api.get(f"/api/v1/cases/{world['case']}/tenant-transfers/preflight",
                      params={"destination_tenant": "dstco"}, headers=_headers("alice"))
    assert r.json()["can_move"] is True, r.json()


async def test_source_keeps_its_threads_detached_and_closed(api, world):
    from velaris_shared.db.models import ChannelConversationModel, ChannelMessageModel

    _, conv_id = await _seed_comms(world)
    out = await _move(api, world)
    assert out["manifest"]["rehomed"]["channel_conversations"] == 1
    assert out["manifest"]["rehomed"]["channel_messages"] == 2
    assert out["manifest"]["rehomed"]["email_messages"] == 1
    async with TestSessionFactory() as db:
        conv = await db.get(ChannelConversationModel, conv_id)
        assert conv.status == "closed" and conv.case_id is None
        assert conv.tenant_id == "srcco" and conv.assigned_to is None   # still the source's
        msgs = (await db.execute(select(ChannelMessageModel))).scalars().all()
        assert all(m.case_id is None and m.tenant_id == "srcco" for m in msgs)
        emails = (await db.execute(select(T["email_messages"]))).all()
        assert all(e.case_id is None and e.tenant_id == "srcco" for e in emails)


async def test_destination_gets_a_read_only_transcript_with_staff_pseudonymised(api, world):
    await _seed_comms(world)
    await _move(api, world)
    async with TestSessionFactory() as db:
        notes = (await db.execute(select(T["case_messages"]).where(
            T["case_messages"].c.author == "velaris:tenant-transfer"))).all()
    assert len(notes) == 1
    body = notes[0].body
    assert notes[0].portal_visible is False
    assert "my claim is late" in body and "looking into it" in body and "please call me" in body
    assert "customer (+447700900123)" in body
    assert "alice" not in body                     # source staff never named
    assert body.index("my claim is late") < body.index("looking into it")   # chronological


async def test_sms_log_moves_with_the_case_without_the_sources_connector(api, world):
    await _seed_comms(world)
    await _move(api, world)
    async with TestSessionFactory() as db:
        rows = (await db.execute(select(T["sms_messages"]))).all()
    assert rows and all(r.tenant_id == "dstco" and r.connector_id is None for r in rows)


async def test_the_customers_next_message_stays_with_the_source(api, world):
    """The point of the whole item: after the move, the source's endpoint must
    not deliver the customer into the moved case — not through the open
    conversation (closed), and not through rung 2 (tenant-scoped now)."""
    from velaris_shared.db.models import ChannelEndpointModel
    from case_service.channels import service as channels

    ep_id, conv_id = await _seed_comms(world)
    await _move(api, world)
    async with TestSessionFactory() as db:
        ep = await db.get(ChannelEndpointModel, ep_id)
        res = await channels.ingest_inbound(
            db, endpoint=ep, peer_address="+447700900123", body="any news?",
            provider_message_id="wamid.after", raw_payload={})
        await db.commit()
    assert res.case_id is None
    assert res.conversation_id != str(conv_id)


async def test_a_reply_to_the_old_email_does_not_reach_the_moved_case(api, world):
    from case_service.mail.threader import resolve_case_id_from_message

    await _seed_comms(world)
    await _move(api, world)
    async with TestSessionFactory() as db:
        found = await resolve_case_id_from_message(
            db, in_reply_to="<m1@src>", references=[], subject="Re: about my claim",
            tenant_id="srcco")
    assert found is None


async def test_rung_two_never_crosses_tenants(world):
    """Pre-existing: rung 2 looked for the customer's email in EVERY tenant's
    cases. A message to another tenant's number must not find this case."""
    from velaris_shared.db.models import ChannelEndpointModel, PortalCustomerModel
    from case_service.channels import service as channels

    await _seed_comms(world)
    async with TestSessionFactory() as db:
        other_ep = ChannelEndpointModel(id=uuid.uuid4(), tenant_id="othco", channel="whatsapp",
                                        provider="twilio", address="+15550009", routing={},
                                        auto_create_case=False)
        db.add(other_ep)
        db.add(PortalCustomerModel(id=uuid.uuid4(), tenant_id=world["oth"].id,
                                   primary_email="cust@example.com", display_name="Cust",
                                   phone="+447700900123"))
        await db.commit()
        res = await channels.ingest_inbound(
            db, endpoint=other_ep, peer_address="+447700900123", body="hello",
            provider_message_id="wamid.other", raw_payload={})
        await db.commit()
    assert res.case_id is None


async def test_a_cancel_frees_the_case(api, world):
    tid = await _approved(api, world)
    r = await api.post(f"/api/v1/case-tenant-transfers/{tid}/cancel", headers=_headers("alice"))
    assert r.json()["state"] == "cancelled"
    assert (await _request(api, world)).status_code == 201


async def test_the_transfer_action_still_refuses_and_points_here(api, world):
    r = await api.post(f"/api/v1/cases/{world['case']}/actions/transfer",
                       json={"params": {"target_type": "tenant", "target_id": "dstco"}},
                       headers=_headers("alice"))
    assert r.status_code == 400 and "tenant-transfers" in r.text


async def test_an_open_data_request_blocks_by_email_or_portal_customer(api, world):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, world["case"])
        case.portal_submitter_email = "Jane@Example.test"
        await db.execute(insert(T["gdpr_requests"]).values(
            id=uuid.uuid4(), subject_id="jane@example.test", request_type="export",
            status="pending"))
        await db.commit()
    r = await _request(api, world)
    assert r.status_code == 409 and "data subject" in r.text

    async with TestSessionFactory() as db:
        await db.execute(T["gdpr_requests"].update().values(status="completed"))
        case = await db.get(CaseInstanceModel, world["case"])
        case.portal_submitter_email = None
        pc_id = uuid.uuid4()
        await db.execute(insert(T["portal_customers"]).values(
            id=pc_id, tenant_id=world["src"].id, primary_email="pc@example.test",
            display_name="Portal Customer"))
        await db.execute(insert(T["portal_customer_cases"]).values(
            customer_id=pc_id, case_id=world["case"]))
        await db.execute(insert(T["gdpr_requests"]).values(
            id=uuid.uuid4(), subject_id=str(pc_id), request_type="erasure", status="pending"))
        await db.commit()
    r = await _request(api, world)
    assert r.status_code == 409 and "data subject" in r.text


async def test_the_case_list_treats_a_caller_with_no_tenant_as_default(api, world):
    """Found in the 2026-09-17 audit: a non-superadmin with no tenant claim
    listed every tenant's cases. It now sees what the single-case read lets it
    see — here, no `default` tenant exists, so no owned case at all."""
    r = await api.get("/api/v1/cases?page_size=100",
                      headers=_headers("nobody-with-no-directory-row"))
    assert r.status_code == 200, r.text
    ids = {c["id"] for c in r.json()["items"]}
    assert str(world["case"]) not in ids
    assert str(world["other"]) not in ids
    # The tenant's own admin still sees its cases.
    r = await api.get("/api/v1/cases?page_size=100", headers=_headers("alice"))
    assert str(world["case"]) in {c["id"] for c in r.json()["items"]}


# ── preflight: the blockers BEFORE the button (Oct item 6) ────────────────────

async def _preflight(api, world, who="alice", dest="dstco"):
    return await api.get(f"/api/v1/cases/{world['case']}/tenant-transfers/preflight",
                         params={"destination_tenant": dest}, headers=_headers(who))


async def test_preflight_says_what_request_would_say_and_writes_nothing(api, world):
    r = await _preflight(api, world)
    assert r.status_code == 200, r.text
    assert r.json()["can_move"] is True and r.json()["blockers"] == []

    async with TestSessionFactory() as db:
        await db.execute(insert(T["payment_requests"]).values(
            id=uuid.uuid4(), tenant_id="srcco", case_id=str(world["case"]), step_id="p",
            provider="stripe", amount_cents=100))
        await db.commit()
    r = await _preflight(api, world)
    assert r.json()["can_move"] is False
    assert any("payment requests" in b for b in r.json()["blockers"])
    req = await _request(api, world)
    assert req.status_code == 409          # the same answer the button would get
    async with TestSessionFactory() as db:
        assert (await db.execute(select(CaseTenantTransferModel))).scalars().all() == []


async def test_preflight_names_an_open_transfer(api, world):
    tid = (await _request(api, world)).json()["id"]
    r = await _preflight(api, world)
    assert r.json()["can_move"] is False and r.json()["open_transfer_id"] == tid


async def test_preflight_has_the_same_gates_as_request(api, world):
    assert (await _preflight(api, world, who="olga")).status_code == 404   # another tenant's case
    assert (await _preflight(api, world, who="dave")).status_code == 404   # the destination
    assert (await _preflight(api, world, dest="no-such-co")).status_code == 404
    r = await api.get(f"/api/v1/cases/{world['case']}/tenant-transfers/preflight",
                      params={"destination_tenant": "dstco"},
                      headers=_headers("alice", roles=("viewer",)))
    assert r.status_code == 403


# ── the comms routes had no case gate (found in 8a, 2026-09-28) ────────────────

async def test_comms_case_routes_need_case_access(api, world):
    await _seed_comms(world)
    case = world["case"]
    for path in (f"/api/v1/comms/sms/cases/{case}/messages",
                 f"/api/v1/comms/slack/cases/{case}/notifications"):
        assert (await api.get(path, headers=_headers("alice"))).status_code == 200
        assert (await api.get(path, headers=_headers("olga"))).status_code in (403, 404)
    r = await api.post(f"/api/v1/comms/sms/cases/{case}/send", headers=_headers("olga"),
                       json={"step_id": "s", "to_number": "+1", "body": "x"})
    assert r.status_code in (403, 404)
    r = await api.post(f"/api/v1/comms/slack/cases/{case}/send", headers=_headers("olga"),
                       json={"step_id": "s", "message": "x"})
    assert r.status_code in (403, 404)


async def test_comms_global_lists_are_tenant_scoped(api, world):
    await _seed_comms(world)            # one SMS for srcco
    async with TestSessionFactory() as db:
        await db.execute(insert(T["sms_messages"]).values(
            id=uuid.uuid4(), tenant_id="othco", case_id=world["other"], step_id="s",
            to_number="+19999", body="othco's text", status="sent"))
        await db.commit()
    mine = (await api.get("/api/v1/comms/sms/messages", headers=_headers("olga"))).json()
    assert [m["to_number"] for m in mine] == ["+19999"]
    assert (await api.get("/api/v1/comms/sms/messages", params={"limit": 100000},
                          headers=_headers("olga"))).status_code == 422


# ── the race: an inbound message arriving WHILE the move commits (PG only) ─────

_PG = pytest.mark.skipif(
    not (os.environ.get("VELARIS_TEST_DATABASE_URL", "").startswith("postgresql")),
    reason="row locks need a real PostgreSQL; SQLite ignores FOR UPDATE")


@_PG
async def test_an_inbound_mid_move_waits_and_opens_a_fresh_thread(world):
    """Session A has closed + detached the conversation but not committed.
    Session B's inbound must wait on the row lock, then see it closed — never
    file the message on the moved case."""
    import asyncio

    from velaris_shared.db.models import ChannelConversationModel, ChannelEndpointModel
    from case_service.channels import service as channels

    ep_id, conv_id = await _seed_comms(world)
    a = TestSessionFactory()
    await a.execute(update(ChannelConversationModel)
                    .where(ChannelConversationModel.id == conv_id)
                    .values(case_id=None, status="closed"))
    # …and, as `accept` does in the same transaction, the case changes tenant.
    await a.execute(update(CaseInstanceModel).where(CaseInstanceModel.id == world["case"])
                    .values(tenant_id=world["dst"].id))

    async def inbound():
        async with TestSessionFactory() as b:
            ep = await b.get(ChannelEndpointModel, ep_id)
            res = await channels.ingest_inbound(
                b, endpoint=ep, peer_address="+447700900123", body="mid-move",
                provider_message_id="wamid.race", raw_payload={})
            await b.commit()
            return res

    task = asyncio.create_task(inbound())
    await asyncio.sleep(0.5)
    assert not task.done(), "the inbound did not wait for the move's lock"
    await a.commit()
    await a.close()
    res = await asyncio.wait_for(task, 10)
    assert res.case_id is None and res.conversation_id != str(conv_id)
