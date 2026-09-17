"""F-12 (first slice) — process-as-negotiation. The capstone.

The roadmap's exit criteria: two instances negotiate, approve, shadow-run and
enforce a two-party workflow; unilateral suspension mid-case queues cleanly; and
cross-anchor verification detects a simulated history rewrite on either side.

The tests that matter most:

* **the matcher is a pure function** — same inputs, same contract hash, on any
  machine, which is the whole of "no trusted third party";
* **deny-overrides survives negotiation by construction** — the seam ceiling is
  the minimum of what was negotiated and what the platform permits, and the
  wider number is never produced;
* a term **agreed** is not a term **in force**: an un-enforced or suspended
  contract carries nothing;
* suspension needs nothing from the counterparty and fail-closes to a **queue**;
* cross-anchoring detects a rewrite **on either side**, symmetrically.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import uuid

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from velaris_shared.db.models import TenantModel

from case_service.handoff import service as handoff
from case_service.negotiate import matcher, service

pytestmark = pytest.mark.asyncio


def _keypair():
    private = ed25519.Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    return private, base64.b64encode(raw).decode()


OUR_OFFER = {
    "fragments": [
        {"slug": "receive-claim", "requires": [], "provides": ["claim_received"]},
        {"slug": "settle", "requires": ["repair_quoted"], "provides": ["settled"]},
    ],
    "response_hours": 4,
    "evidence": ["anchor_exchange", "ledger"],
}
THEIR_OFFER = {
    "fragments": [
        {"slug": "quote-repair", "requires": ["claim_received"],
         "provides": ["repair_quoted"]},
    ],
    "response_hours": 8,
    "evidence": ["anchor_exchange"],
}
OUR_REQ = {"goals": ["settled"], "max_ceiling": "personal",
           "regions": ["eu", "uk"], "max_response_hours": 12,
           "evidence": ["anchor_exchange"]}
THEIR_REQ = {"goals": ["repair_quoted"], "max_ceiling": "internal",
             "regions": ["uk"], "max_response_hours": 6,
             "evidence": ["ledger"]}


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _counterparty(session, tenant, *, max_ceiling="personal"):
    private, public = _keypair()
    row = await handoff.enrol(
        session, tenant_uuid=tenant.id, slug="repairer",
        display_name="Repair Co", public_key=public, max_ceiling=max_ceiling,
        created_by="alice")
    await session.commit()
    return row, private


async def _propose(session, tenant, cp, **over):
    kwargs = dict(tenant_uuid=tenant.id, counterparty=cp, name="Claims seam",
                  our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                  our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                  created_by="alice")
    kwargs.update(over)
    row = await service.propose(session, **kwargs)
    await session.commit()
    return row


# ══ the matcher is a pure function ══════════════════════════════════════════


def test_the_same_inputs_give_the_same_contract():
    """The whole of "no trusted third party": either side can recompute the
    other's answer instead of trusting it."""
    one = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                        platform_ceiling="personal")
    two = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                        platform_ceiling="personal")
    assert one["contract_sha256"] == two["contract_sha256"]
    assert one["matched"] is True


def test_the_ceiling_is_the_minimum_of_both_sides():
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                        platform_ceiling="personal")
    # We would send `personal`; they accept at most `internal`.
    assert out["effective_ceiling"] == "internal"


def test_deny_overrides_survives_negotiation_by_construction():
    """A contract cannot widen what the platform allows — not because a check
    runs at the right moment, but because the wider number is never produced."""
    out = matcher.match(
        our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
        our_requirements={**OUR_REQ, "max_ceiling": "personal"},
        their_requirements={**THEIR_REQ, "max_ceiling": "personal"},
        platform_ceiling="none")
    assert out["effective_ceiling"] == "none"
    clause = next(c for c in out["resolution"]["clauses"]
                  if c["clause"] == "data_class_ceiling")
    assert clause["negotiated"] == "personal" and clause["agreed"] == "none"


def test_article_9_is_not_a_negotiable_term():
    with pytest.raises(matcher.MatchError, match="not negotiable"):
        matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                      our_requirements={**OUR_REQ, "max_ceiling": "special_category"},
                      their_requirements=THEIR_REQ, platform_ceiling="personal")


def test_no_shared_region_is_unmatchable():
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements={**OUR_REQ, "regions": ["us"]},
                        their_requirements=THEIR_REQ, platform_ceiling="personal")
    assert out["matched"] is False
    assert "no region both sides" in out["reason"]


def test_a_promise_slower_than_the_requirement_is_unmatchable():
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements={**OUR_REQ, "max_response_hours": 2},
                        their_requirements=THEIR_REQ, platform_ceiling="personal")
    assert out["matched"] is False and "8" in out["reason"]


def test_an_evidence_obligation_nobody_offers_is_unmatchable():
    out = matcher.match(
        our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
        our_requirements={**OUR_REQ, "evidence": ["notarised_by_a_third_party"]},
        their_requirements=THEIR_REQ, platform_ceiling="personal")
    assert out["matched"] is False and "notarised" in out["reason"]


def test_a_goal_nobody_offers_is_unmatchable():
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements={**OUR_REQ, "goals": ["world_peace"]},
                        their_requirements=THEIR_REQ, platform_ceiling="personal")
    assert out["matched"] is False and "world_peace" in out["reason"]


def test_the_composition_interleaves_both_sides_and_marks_the_seams():
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                        platform_ceiling="personal")
    steps = out["composition"]["steps"]
    assert [s["fragment"] for s in steps] == [
        "receive-claim", "quote-repair", "settle"]
    assert [s["side"] for s in steps] == ["ours", "theirs", "ours"]
    # Two boundary crossings: ours→theirs and theirs→ours.
    assert out["composition"]["seams"] == 2


def test_every_clause_is_reported_including_the_ones_that_passed():
    """A negotiation somebody has to sign is not a place for a single green
    tick."""
    out = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                        our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                        platform_ceiling="personal")
    names = [c["clause"] for c in out["resolution"]["clauses"]]
    assert names == ["data_class_ceiling", "regions", "their_sla", "our_sla",
                     "evidence", "composition"]


def test_the_hash_covers_the_composition_not_the_bookkeeping():
    """A hash over the row would still match if somebody edited the composition
    and left the other columns alone."""
    base = matcher.match(our_offer=OUR_OFFER, their_offer=THEIR_OFFER,
                         our_requirements=OUR_REQ, their_requirements=THEIR_REQ,
                         platform_ceiling="personal")
    moved = dict(base["composition"])
    moved["steps"] = list(reversed(moved["steps"]))
    assert matcher.contract_hash(moved, base["resolution"]) != base["contract_sha256"]


# ══ the lifecycle ═══════════════════════════════════════════════════════════


async def test_an_unmatchable_contract_is_still_a_record(session, tenant):
    """"These two sets of requirements have no joint process" is the result of a
    real negotiation, and the failing clause is what the next round must move."""
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp,
                         our_requirements={**OUR_REQ, "regions": ["us"]})
    assert row.status == "unmatchable"
    assert row.unmatched_reason and row.contract_sha256 is None


async def test_a_contract_only_one_side_signed_cannot_be_approved(session, tenant):
    """Approving it would be agreeing to something the other party has not yet
    said they will do."""
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    assert row.status == "matched" and row.our_signature
    with pytest.raises(service.NegotiationError, match="both sides sign"):
        await service.approve(session, row=row, actor="alice")


async def test_a_forged_counter_signature_is_refused(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    impostor = ed25519.Ed25519PrivateKey.generate()
    forged = base64.b64encode(impostor.sign(row.contract_sha256.encode())).decode()
    with pytest.raises(service.NegotiationError, match="does not verify"):
        await service.counter_sign(session, row=row, signature=forged)


async def test_both_signatures_then_approval(session, tenant):
    cp, private = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    signature = base64.b64encode(private.sign(row.contract_sha256.encode())).decode()
    await service.counter_sign(session, row=row, signature=signature)
    await service.approve(session, row=row, actor="alice")
    assert row.status == "approved" and row.approved_by == "alice"


async def _approved(session, tenant):
    cp, private = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    signature = base64.b64encode(private.sign(row.contract_sha256.encode())).decode()
    await service.counter_sign(session, row=row, signature=signature)
    await service.approve(session, row=row, actor="alice")
    await session.commit()
    return row


async def test_shadow_cannot_be_skipped(session, tenant):
    """Skipping shadow would make the first real case the experiment."""
    row = await _approved(session, tenant)
    with pytest.raises(service.NegotiationError, match="one stage at a time"):
        await service.promote(session, row=row, to="enforced", actor="alice")


async def test_the_full_lifecycle(session, tenant):
    row = await _approved(session, tenant)
    await service.promote(session, row=row, to="shadow", actor="alice")
    assert service.seam_ceiling(row) == "internal"
    await service.promote(session, row=row, to="enforced", actor="alice")
    assert row.status == "enforced" and row.enforced_at is not None


async def test_a_term_agreed_is_not_a_term_in_force(session, tenant):
    """Reading the negotiated ceiling directly is the mistake `seam_ceiling`
    exists to prevent."""
    row = await _approved(session, tenant)
    assert row.effective_ceiling == "internal"
    # Approved but not running: the seam carries nothing.
    assert service.seam_ceiling(row) == "none"


# ══ the kill switch ═════════════════════════════════════════════════════════


async def test_suspension_needs_nothing_from_the_counterparty(session, tenant):
    row = await _approved(session, tenant)
    await service.promote(session, row=row, to="shadow", actor="alice")
    out = await service.suspend(session, row=row, actor="bob", reason="incident")
    assert row.status == "suspended" and row.suspended_by == "bob"
    assert out["seam"] == service.SUSPENDED_BEHAVIOUR


async def test_suspension_fail_closes_to_a_queue_not_an_error(session, tenant):
    """A counterparty mid-case must not be left holding work it cannot hand
    back."""
    row = await _approved(session, tenant)
    out = await service.suspend(session, row=row, actor="bob", reason="incident")
    assert out["seam"] == "proposals_queued"
    assert "nothing in flight is stranded" in out["note"]
    assert service.seam_ceiling(row) == "none"


async def test_suspending_twice_is_not_an_error(session, tenant):
    row = await _approved(session, tenant)
    await service.suspend(session, row=row, actor="bob", reason="incident")
    out = await service.suspend(session, row=row, actor="bob", reason="again")
    assert out.get("already") is True


# ══ verification without a third party ══════════════════════════════════════


async def test_either_side_can_recompute_the_contract(session, tenant):
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    out = service.verify_their_match(row, platform_ceiling=cp.max_ceiling)
    assert out["agrees"] is True


async def test_a_contract_edited_after_the_match_does_not_recompute(session, tenant):
    """A mismatch is a disagreement about the contract itself, not about it."""
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    composition = dict(row.composition)
    composition["steps"] = [*composition["steps"],
                            {"side": "theirs", "fragment": "smuggled",
                             "provides": [], "crosses_seam": False}]
    row.composition = composition
    await session.commit()
    out = service.verify_their_match(row, platform_ceiling=cp.max_ceiling)
    assert out["agrees"] is False
    # The INPUTS still produce the original hash — nothing was renegotiated.
    # What changed is the stored composition, and only re-hashing what is
    # actually stored catches that. This test found the gap.
    assert out["inputs_agree"] is True and out["stored_agrees"] is False
    assert "edited after both sides signed" in out["detail"]


async def test_renegotiating_behind_the_hash_is_also_caught(session, tenant):
    """The other half: terms changed, composition left alone."""
    cp, _ = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    row.their_requirements = {**THEIR_REQ, "max_ceiling": "none"}
    await session.commit()
    out = service.verify_their_match(row, platform_ceiling=cp.max_ceiling)
    assert out["agrees"] is False and out["inputs_agree"] is False


# ══ cross-anchoring ═════════════════════════════════════════════════════════


async def test_anchors_are_recorded_in_both_directions(session, tenant):
    row = await _approved(session, tenant)
    await service.anchor(session, row=row, direction="ours", chain_head="a" * 64)
    await service.anchor(session, row=row, direction="theirs", chain_head="b" * 64)
    await session.commit()
    out = await service.audit_anchors(session, row)
    assert out["ours"] == 1 and out["theirs"] == 1 and out["clean"] is True


async def test_a_repeated_head_is_recorded_as_a_divergence(session, tenant):
    """Nothing was added to that side's history since the last exchange, which
    is worth noticing."""
    row = await _approved(session, tenant)
    await service.anchor(session, row=row, direction="theirs", chain_head="c" * 64)
    await service.anchor(session, row=row, direction="theirs", chain_head="c" * 64)
    await session.commit()
    out = await service.audit_anchors(session, row)
    assert out["clean"] is False
    assert "anchored twice" in out["problems"][0]["detail"]


async def test_an_unsigned_rewrite_is_detected_not_rejected(session, tenant):
    """Refusing it would leave no trace of the thing we most need evidence of.
    Detection is a row."""
    cp, private = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    signature = base64.b64encode(private.sign(row.contract_sha256.encode())).decode()
    await service.counter_sign(session, row=row, signature=signature)
    await service.approve(session, row=row, actor="alice")

    impostor = ed25519.Ed25519PrivateKey.generate()
    head = "d" * 64
    forged = base64.b64encode(impostor.sign(head.encode())).decode()
    anchor = await service.anchor(session, row=row, direction="theirs",
                                  chain_head=head, signature=forged)
    await session.commit()
    assert anchor.divergence and "pinned key" in anchor.divergence
    out = await service.audit_anchors(session, row)
    assert out["clean"] is False


async def test_a_genuinely_signed_anchor_is_clean(session, tenant):
    cp, private = await _counterparty(session, tenant)
    row = await _propose(session, tenant, cp)
    head = "e" * 64
    signature = base64.b64encode(private.sign(head.encode())).decode()
    anchor = await service.anchor(session, row=row, direction="theirs",
                                  chain_head=head, signature=signature)
    assert anchor.divergence is None


async def test_the_audit_is_symmetric(session, tenant):
    """The value is not that we can prove what they did — it is that neither of
    us can deny it."""
    row = await _approved(session, tenant)
    await service.anchor(session, row=row, direction="ours", chain_head="1" * 64)
    await service.anchor(session, row=row, direction="ours", chain_head="1" * 64)
    await session.commit()
    out = await service.audit_anchors(session, row)
    assert out["problems"][0]["direction"] == "ours"


# ══ renegotiation ═══════════════════════════════════════════════════════════


async def test_renegotiation_is_a_new_row_never_an_edit(session, tenant):
    """What both sides signed must stay exactly as it was signed."""
    cp, _ = await _counterparty(session, tenant)
    first = await _propose(session, tenant, cp)
    second = await _propose(session, tenant, cp, supersedes=first.id,
                            their_requirements={**THEIR_REQ, "max_ceiling": "personal"})
    assert second.version == 2 and second.supersedes == first.id
    assert first.contract_sha256 != second.contract_sha256
    assert second.effective_ceiling == "personal"


async def test_the_api_is_gated(anon_client):
    resp = await anon_client.get("/api/v1/negotiate/contracts")
    assert resp.status_code in (401, 403)
    resp = await anon_client.post("/api/v1/negotiate/contracts", json={})
    assert resp.status_code in (401, 403, 422)
