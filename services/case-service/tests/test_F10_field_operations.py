"""F-10 (first slice) — offline-sovereign field operations, the hub half.

The roadmap's exit criteria are convergence with deterministic conflict
outcomes, sub-chain verification passing, and a stolen-node drill where the
shred order and the staleness lockout are both verified. All of them are here,
exercised against a simulated node — the field-node deployment profile itself is
not built, and is named as not built rather than implied.

The tests that matter most:

* a **broken sub-chain is refused in full**, not merged up to the break;
* an event about a case **outside the charter** is rejected — a stolen node
  cannot contain, or contribute, what it was never given;
* work recorded **after the node should have stopped** is refused;
* **nothing sensitive merges itself** — a disagreement on a personal field goes
  to a person, always;
* the shred order **rides home on the next contact**, and the staleness lockout
  covers the node that never comes back.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    FieldSyncEventModel,
    TenantModel,
)

from case_service.field import service, subchain
from case_service.handoff import projection as proj

pytestmark = pytest.mark.asyncio


def _keypair():
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return private, base64.b64encode(raw).decode()


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _case_type(session, tenant, name="Claims"):
    ct = CaseTypeModel(id=uuid.uuid4(), name=name, version="1.0.0",
                       lifecycle_process_id="p", definition_json={"stages": []},
                       tenant_id=str(tenant.id))
    session.add(ct)
    await session.flush()
    return ct


async def _case(session, tenant, ct, data=None):
    case = CaseInstanceModel(
        id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", tenant_id=tenant.id,
        data=dict(data or {"reference": "R-1", "customer_email": "a@example.test"}))
    session.add(case)
    await session.commit()
    return case


async def _node(session, tenant, ct, **over):
    _, public = _keypair()
    kwargs = dict(tenant_uuid=tenant.id, slug="ship-a", display_name="Ship A",
                  public_key=public, charter={"case_type_ids": [str(ct.id)]},
                  ceiling=proj.CEILING_INTERNAL)
    kwargs.update(over)
    node = await service.register(session, **kwargs)
    await session.commit()
    return node


# ══ the sub-chain ═══════════════════════════════════════════════════════════


def test_a_clean_chain_verifies():
    events = subchain.build([
        {"case_id": "c", "kind": "note", "payload": {"text": "one"}},
        {"case_id": "c", "kind": "note", "payload": {"text": "two"}},
    ], "GEN")
    ok, head, reason = subchain.verify(events, "GEN")
    assert ok and head == events[-1]["content_hash"] and reason == ""


def test_an_edited_payload_breaks_the_chain():
    events = subchain.build([
        {"case_id": "c", "kind": "note", "payload": {"text": "one"}},
        {"case_id": "c", "kind": "note", "payload": {"text": "two"}},
    ], "GEN")
    events[0]["payload"] = {"text": "something else"}
    ok, _, reason = subchain.verify(events, "GEN")
    assert not ok and "payload" in reason


def test_a_removed_event_breaks_the_chain():
    """A gap is a lost stream, not a late one."""
    events = subchain.build([
        {"case_id": "c", "kind": "note", "payload": {"text": str(i)}}
        for i in range(3)], "GEN")
    ok, _, reason = subchain.verify([events[0], events[2]], "GEN")
    assert not ok and "sequence" in reason


def test_a_chain_from_another_genesis_is_not_this_dispatchs():
    """The check that stops a node replaying an old period's work."""
    events = subchain.build([{"case_id": "c", "kind": "note", "payload": {}}], "OTHER")
    ok, _, reason = subchain.verify(events, "GEN")
    assert not ok and "follow" in reason


# ══ the charter ═════════════════════════════════════════════════════════════


async def test_a_node_with_an_open_charter_is_refused(session, tenant):
    """A node that may hold anything is the opposite of what a charter is for."""
    _, public = _keypair()
    with pytest.raises(service.FieldError, match="charter"):
        await service.register(session, tenant_uuid=tenant.id, slug="n",
                               display_name="N", public_key=public, charter={})


async def test_a_dispatch_carries_only_charter_cases(session, tenant):
    inside = await _case_type(session, tenant, "Inside")
    outside = await _case_type(session, tenant, "Outside")
    mine = await _case(session, tenant, inside)
    await _case(session, tenant, outside)
    node = await _node(session, tenant, inside)
    row, bundle = await service.dispatch(session, node=node,
                                         tenant_token=str(tenant.id))
    assert row.case_ids == [str(mine.id)]
    assert len(bundle["cases"]) == 1


async def test_work_about_a_case_outside_the_charter_is_rejected(session, tenant):
    """The check that matters. The dispatch check only decides what a
    well-behaved node holds."""
    inside = await _case_type(session, tenant, "Inside")
    outside = await _case_type(session, tenant, "Outside")
    await _case(session, tenant, inside)
    stranger = await _case(session, tenant, outside)
    node = await _node(session, tenant, inside)
    row, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    await session.commit()

    events = subchain.build(
        [{"case_id": str(stranger.id), "kind": "note", "payload": {"text": "hi"}}],
        row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    assert out["rejected"] == 1 and out["applied"] == 0
    stored = (await session.execute(select(FieldSyncEventModel))).scalars().all()
    assert "outside the node's charter" in stored[0].conflict_reason


async def test_a_node_may_not_record_platform_changes(session, tenant):
    """A node reports what a worker did to a CASE, never what an administrator
    did to the platform."""
    ct = await _case_type(session, tenant)
    case = await _case(session, tenant, ct)
    node = await _node(session, tenant, ct)
    row, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    await session.commit()
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "grant_role", "payload": {"role": "admin"}}],
        row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    assert out["rejected"] == 1


# ══ the projection — a node is as untrusted as an external agent ════════════


async def test_a_dispatch_withholds_what_the_ceiling_forbids(session, tenant):
    ct = await _case_type(session, tenant)
    await _case(session, tenant, ct,
                data={"reference": "R-1", "customer_email": "a@example.test",
                      "diagnosis_notes": "never"})
    node = await _node(session, tenant, ct)
    _, bundle = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    fields = bundle["cases"][0]["fields"]
    assert "reference" in fields
    assert "customer_email" not in fields
    assert "diagnosis_notes" not in fields


async def test_the_dispatch_record_keeps_names_not_values(session, tenant):
    """This row answers "what was on that device" and is read by anybody who may
    read the node's history."""
    ct = await _case_type(session, tenant)
    await _case(session, tenant, ct,
                data={"reference": "R-1", "customer_email": "secret@example.test"})
    node = await _node(session, tenant, ct)
    row, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    blob = str(row.projection)
    assert "customer_email" in blob
    assert "secret@example.test" not in blob


# ══ merging ═════════════════════════════════════════════════════════════════


async def _dispatched(session, tenant, data=None):
    ct = await _case_type(session, tenant)
    case = await _case(session, tenant, ct, data=data)
    node = await _node(session, tenant, ct)
    row, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    await session.commit()
    return node, row, case


async def test_a_broken_chain_merges_nothing_at_all(session, tenant):
    """Merging the part before the break would accept exactly the events an
    attacker chose to leave alone."""
    node, row, case = await _dispatched(session, tenant)
    events = subchain.build([
        {"case_id": str(case.id), "kind": "note", "payload": {"text": "one"}},
        {"case_id": str(case.id), "kind": "note", "payload": {"text": "two"}},
    ], row.subchain_genesis)
    events[0]["payload"] = {"text": "tampered"}
    with pytest.raises(service.FieldError, match="does not verify"):
        await service.sync(session, node=node, dispatch_row=row, events=events)
    assert (await session.execute(select(FieldSyncEventModel))).scalars().all() == []


async def test_notes_are_additive_and_never_conflict(session, tenant):
    node, row, case = await _dispatched(session, tenant)
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "note", "payload": {"text": "seen"}}],
        row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    assert out["applied"] == 1 and out["conflicts"] == 0


async def test_a_field_the_hub_never_had_merges_cleanly(session, tenant):
    node, row, case = await _dispatched(session, tenant, data={"reference": "R-1"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "survey_code",
          "payload": {"value": "XYZ"}}], row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    await session.refresh(case)
    assert out["applied"] == 1
    assert case.data["survey_code"] == "XYZ"


async def test_nothing_sensitive_merges_itself(session, tenant):
    """A disagreement on a personal field goes to a person, always. There is no
    precedence rule that overwrites somebody's contact details from a device
    that has been out of contact for three days."""
    node, row, case = await _dispatched(
        session, tenant, data={"customer_email": "hub@example.test"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "customer_email",
          "payload": {"value": "field@example.test"}}], row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    await session.refresh(case)
    assert out["conflicts"] == 1
    # Untouched until a person decides.
    assert case.data["customer_email"] == "hub@example.test"


async def test_an_ordinary_disagreement_is_still_a_person_s_decision(session, tenant):
    """The hub's value stands, and the queue says so — but a person can choose
    the field's, which is why it is a queue and not a silent overwrite."""
    node, row, case = await _dispatched(session, tenant, data={"reference": "HUB"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "reference",
          "payload": {"value": "FIELD"}}], row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    assert out["conflicts"] == 1
    stored = (await session.execute(select(FieldSyncEventModel))).scalars().first()
    assert "hub's value stands" in stored.conflict_reason


async def test_an_identical_value_is_not_a_conflict(session, tenant):
    node, row, case = await _dispatched(session, tenant, data={"reference": "SAME"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "reference",
          "payload": {"value": "SAME"}}], row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    assert out["applied"] == 1 and out["conflicts"] == 0


async def test_resolving_keep_field_writes_the_node_s_value(session, tenant):
    node, row, case = await _dispatched(session, tenant, data={"reference": "HUB"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "reference",
          "payload": {"value": "FIELD"}}], row.subchain_genesis)
    await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    conflict = (await session.execute(select(FieldSyncEventModel))).scalars().first()
    await service.resolve(session, event=conflict, resolution="keep_field",
                          actor="alice")
    await session.commit()
    await session.refresh(case)
    assert case.data["reference"] == "FIELD"
    assert conflict.merge_state == "applied" and conflict.resolved_by == "alice"


async def test_resolving_keep_hub_leaves_the_case_alone(session, tenant):
    node, row, case = await _dispatched(session, tenant, data={"reference": "HUB"})
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "variable_set", "target": "reference",
          "payload": {"value": "FIELD"}}], row.subchain_genesis)
    await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    conflict = (await session.execute(select(FieldSyncEventModel))).scalars().first()
    await service.resolve(session, event=conflict, resolution="keep_hub", actor="alice")
    await session.commit()
    await session.refresh(case)
    assert case.data["reference"] == "HUB"


# ══ the stolen-node drill ═══════════════════════════════════════════════════


async def test_work_recorded_after_the_lockout_is_refused(session, tenant):
    """A node that ignored its own lockout is the node whose work must not be
    trusted."""
    node, row, case = await _dispatched(session, tenant)
    row.stale_after = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "note", "payload": {"text": "late"},
          "node_time": datetime.now(timezone.utc).isoformat()}],
        row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    assert out["rejected"] == 1


async def test_a_node_that_never_returns_is_locked_out_by_staleness(session, tenant):
    node, row, _ = await _dispatched(session, tenant)
    later = datetime.now(timezone.utc) + timedelta(hours=999)
    state = service.staleness(node, row, now=later)
    assert state["stale"] is True and "fail closed" in state["reason"]


async def test_the_shred_order_rides_home_on_the_next_contact(session, tenant):
    node, row, case = await _dispatched(session, tenant)
    await service.order_shred(session, node=node, reason="device lost overboard")
    await session.commit()
    assert node.status == "shred_ordered"

    events = subchain.build(
        [{"case_id": str(case.id), "kind": "note", "payload": {"text": "last"}}],
        row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    assert out["shred_ordered"] is True
    assert "Wipe the node now" in out["note"]
    assert node.status == "shredded" and node.shredded_at is not None


async def test_a_shredded_node_may_not_sync_again(session, tenant):
    node, row, case = await _dispatched(session, tenant)
    node.status = "shredded"
    await session.commit()
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "note", "payload": {}}],
        row.subchain_genesis)
    with pytest.raises(service.FieldError, match="shredded"):
        await service.sync(session, node=node, dispatch_row=row, events=events)


# ══ convergence ═════════════════════════════════════════════════════════════


async def test_a_full_round_trip_converges_and_anchors_the_subchain(session, tenant):
    """The exit criterion, minus the second real node: dispatch, work offline,
    sync, converge, and the hub anchors the head it verified."""
    node, row, case = await _dispatched(
        session, tenant, data={"reference": "R-1"})
    events = subchain.build([
        {"case_id": str(case.id), "kind": "note", "payload": {"text": "boarded"}},
        {"case_id": str(case.id), "kind": "variable_set", "target": "inspection_code",
         "payload": {"value": "OK-7"}},
        {"case_id": str(case.id), "kind": "step_complete", "target": "s1",
         "payload": {}},
    ], row.subchain_genesis)
    out = await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    await session.refresh(case)
    await session.refresh(node)

    assert out == {**out, "verified": True, "applied": 3, "conflicts": 0,
                   "rejected": 0}
    assert case.data["inspection_code"] == "OK-7"
    assert node.subchain_head == events[-1]["content_hash"]
    assert row.status == "synced"


async def test_the_same_dispatch_cannot_be_synced_twice(session, tenant):
    """Found live. Without this a node hands the same stream back twice and the
    hub records it twice — every note duplicated, every conflict queued again
    for a person who already decided it."""
    node, row, case = await _dispatched(session, tenant)
    events = subchain.build(
        [{"case_id": str(case.id), "kind": "note", "payload": {"text": "one"}}],
        row.subchain_genesis)
    await service.sync(session, node=node, dispatch_row=row, events=events)
    await session.commit()
    with pytest.raises(service.FieldError, match="synced once"):
        await service.sync(session, node=node, dispatch_row=row, events=events)


async def test_a_second_dispatch_supersedes_an_unsynced_one(session, tenant):
    """Two live dispatches means two sub-chains and no way to say which one the
    node is continuing."""
    ct = await _case_type(session, tenant)
    await _case(session, tenant, ct)
    node = await _node(session, tenant, ct)
    first, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    await session.commit()
    second, _ = await service.dispatch(session, node=node, tenant_token=str(tenant.id))
    await session.commit()
    assert first.status == "superseded" and second.status == "issued"
    assert first.subchain_genesis != second.subchain_genesis


# ══ the API ═════════════════════════════════════════════════════════════════


async def test_the_field_api_is_gated(anon_client):
    resp = await anon_client.get("/api/v1/field/nodes")
    assert resp.status_code in (401, 403)
    resp = await anon_client.get("/api/v1/field/conflicts")
    assert resp.status_code in (401, 403)
