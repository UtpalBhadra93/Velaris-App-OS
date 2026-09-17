"""Item 12 — Customer Engagement: the rep desk.

The customer's question was *"when someone calls, will the rep see the
customer's details, and to what extent?"*, and the tests that matter are the
ones about **to what extent**:

* the 360 can only ever NARROW what HxGuard already permits — a rep with no
  `case.read` sees no cases even at Full;
* a panel the rep may not read is **absent, not empty**, because an empty panel
  says "this customer has none of these" and a rep will act on that;
* Anonymous shows an initial and a count, and nothing else;
* a field unmasks **one at a time, with a reason**, and only at Full;
* **never auto-create a profile from an unverified inbound number**;
* a failed verification says one thing whatever failed;
* a warm transfer **carries the tier**, so the customer is not re-verified;
* the feature is dark unless the platform released it **and** this tenant
  installed it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    CustomerDisclosureModel,
    CustomerIdentifierModel,
    CustomerInteractionModel,
    CustomerLinkModel,
    CustomerProfileModel,
    MarketplaceInstallModel,
    ScheduledReleaseModel,
    TenantModel,
)

from case_service.engage import entitlement, identity, interactions, three_sixty

pytestmark = pytest.mark.asyncio

ALL_GRANTS = {a: True for a in three_sixty.PANEL_ACTIONS.values()}


@pytest_asyncio.fixture
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _entitle(session, tenant):
    """Both gates on: the platform released it, and this tenant installed it."""
    session.add(ScheduledReleaseModel(
        id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
        title="Customer Engagement", status="released", enabled="true"))
    session.add(MarketplaceInstallModel(
        id=uuid.uuid4(), tenant_id="default", package_id=entitlement.PACKAGE_ID,
        package_version="1.0.0", package_type="module", approved_by="alice"))
    await session.commit()


async def _profile(session, tenant, name="Jane Smith", **over):
    row = await identity.create_profile(
        session, tenant_uuid=tenant.id, display_name=name, **over)
    await session.commit()
    return row


# ══ the two gates ═══════════════════════════════════════════════════════════


async def test_the_feature_is_dark_until_both_gates_are_open(session, tenant):
    allowed, why = await entitlement.enabled_for(session, "default")
    assert allowed is False and "released" in why


async def test_released_but_not_installed_is_still_dark(session, tenant):
    """An install is a TENANT's decision. A platform-wide flip would switch a
    customer 360 on for every tenant on a shared box."""
    session.add(ScheduledReleaseModel(
        id=uuid.uuid4(), feature_key=entitlement.FEATURE_KEY,
        title="Customer Engagement", status="released", enabled="true"))
    await session.commit()
    allowed, why = await entitlement.enabled_for(session, "default")
    assert allowed is False and "has not installed" in why


async def test_installed_but_not_released_is_dark_too(session, tenant):
    """The release flag is the OUTER gate: a tenant cannot install their way
    into a feature the platform has not shipped."""
    session.add(MarketplaceInstallModel(
        id=uuid.uuid4(), tenant_id="default", package_id=entitlement.PACKAGE_ID,
        package_version="1.0.0", package_type="module", approved_by="alice"))
    await session.commit()
    allowed, why = await entitlement.enabled_for(session, "default")
    assert allowed is False and "released" in why


async def test_both_gates_open(session, tenant):
    await _entitle(session, tenant)
    allowed, _ = await entitlement.enabled_for(session, "default")
    assert allowed is True


# ══ normalisation ═══════════════════════════════════════════════════════════


def test_two_reps_typing_the_same_number_reach_the_same_party():
    forms = ["+44 7700 900000", "+447700900000", "0044 7700 900000",
             "+44-7700-900000"]
    assert len({identity.normalise("phone", f) for f in forms}) == 1


def test_email_is_lowercased():
    assert identity.normalise("email", " Jane@Example.TEST ") == "jane@example.test"


def test_a_national_id_is_never_stored_in_the_clear():
    """A national id in an identifier table is a breach waiting for a SELECT."""
    out = identity.normalise("national_id_hash", "AB123456C")
    assert "AB123456C" not in out and len(out) == 64


def test_a_value_that_cannot_be_normalised_is_refused():
    with pytest.raises(identity.IdentityError):
        identity.normalise("phone", "hello")
    with pytest.raises(identity.IdentityError):
        identity.normalise("email", "not-an-email")


# ══ the spine ═══════════════════════════════════════════════════════════════


async def test_a_profile_is_a_party_not_a_person(session, tenant):
    """Decision 1: households and businesses without a migration."""
    house = await _profile(session, tenant, "The Smiths", party_kind="household")
    jane = await _profile(session, tenant, "Jane Smith")
    await identity.relate(session, tenant_uuid=tenant.id, from_id=jane.id,
                          to_id=house.id, relation="member_of")
    await session.commit()
    assert house.party_kind == "household"


async def test_a_household_member_may_not_automatically_act_for_it(session, tenant):
    """Recorded rather than inferred from the relation name."""
    house = await _profile(session, tenant, "The Smiths", party_kind="household")
    jane = await _profile(session, tenant, "Jane")
    rel = await identity.relate(session, tenant_uuid=tenant.id, from_id=jane.id,
                                to_id=house.id, relation="member_of")
    assert rel.may_act_for is False


async def test_a_profile_mastered_elsewhere_must_name_the_system(session, tenant):
    """A profile mastered by "somewhere else" is one nobody can refresh."""
    with pytest.raises(identity.IdentityError, match="must name the system"):
        await identity.create_profile(
            session, tenant_uuid=tenant.id, display_name="X",
            source_of_truth="crm_mastered")


async def test_a_never_refreshed_crm_profile_is_STALE_not_fresh(session, tenant):
    """A NULL must not read as "up to date" — that is what makes a cache
    dangerous."""
    row = await _profile(session, tenant, "From CRM",
                         source_of_truth="crm_mastered", external_system="salesforce")
    out = identity.staleness(row)
    assert out["stale"] is True and "never refreshed" in out["reason"]


async def test_a_velaris_owned_profile_is_never_stale(session, tenant):
    row = await _profile(session, tenant)
    assert identity.staleness(row)["stale"] is False


async def test_an_identifier_claimed_by_another_party_is_refused(session, tenant):
    """Silently reassigning it would hide the duplicate instead of resolving
    it."""
    one = await _profile(session, tenant, "One")
    two = await _profile(session, tenant, "Two")
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=one.id, kind="phone",
                                  value="+447700900000")
    await session.commit()
    with pytest.raises(identity.IdentityError, match="Merge the two"):
        await identity.add_identifier(session, tenant_uuid=tenant.id,
                                      customer_id=two.id, kind="phone",
                                      value="+44 7700 900000")


# ══ the resolution ladder ═══════════════════════════════════════════════════


async def test_an_exact_identifier_is_rung_one(session, tenant):
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="phone",
                                  value="+447700900000", verified=True)
    await session.commit()
    out = await identity.resolve(session, tenant_uuid=tenant.id,
                                 tenant_token="default", kind="phone",
                                 value="0044 7700 900000")
    assert out["rung"] == 1 and out["customer_id"] == str(row.id)
    assert out["confidence"] == "identifier"


async def test_an_unknown_number_never_creates_a_profile(session, tenant):
    """THE rule. Creating one manufactures a person out of a wrong number, and
    that person then accumulates somebody else's calls."""
    out = await identity.resolve(session, tenant_uuid=tenant.id,
                                 tenant_token="default", kind="phone",
                                 value="+447700111222")
    await session.commit()
    assert out["resolved"] is False and out["rung"] == 5
    assert (await session.execute(select(CustomerProfileModel))).scalars().all() == []


async def test_an_adopted_identifier_is_a_suggestion_not_proof(session, tenant):
    """Adoption writes `verified = False`; nothing reads it as proof."""
    from velaris_shared.db.models import CaseTypeModel, CaseInstanceModel

    ct = CaseTypeModel(id=uuid.uuid4(), name="C", version="1.0.0",
                       lifecycle_process_id="p", definition_json={"stages": []},
                       tenant_id=str(tenant.id))
    session.add(ct)
    await session.flush()
    session.add(CaseInstanceModel(
        id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", tenant_id=tenant.id,
        portal_submitter_email="jane@example.test",
        portal_submitter_name="Jane Smith"))
    await session.commit()

    out = await identity.resolve(session, tenant_uuid=tenant.id,
                                 tenant_token="default", kind="email",
                                 value="Jane@Example.test")
    await session.commit()
    assert out["resolved"] is True and out["rung"] == 3
    assert out["verified"] is False and "SUGGESTION" in out["reason"]
    ident = (await session.execute(select(CustomerIdentifierModel))).scalars().first()
    assert ident.verified is False and ident.source == "resolution"


# ══ merge ═══════════════════════════════════════════════════════════════════


async def test_a_merge_is_a_record_never_a_deletion(session, tenant):
    one = await _profile(session, tenant, "Jane S")
    two = await _profile(session, tenant, "J Smith")
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=two.id, kind="email",
                                  value="j@example.test")
    await session.commit()
    await identity.merge(session, tenant_uuid=tenant.id, winner_id=one.id,
                         loser_id=two.id, reason="same person, two spellings",
                         merged_by="alice")
    await session.commit()
    assert two.status == "merged" and two.merged_into_id == one.id
    # The loser is still readable, and redirects.
    assert (await identity.follow_merges(session, two.id)).id == one.id


async def test_a_merge_needs_a_reason_somebody_can_read(session, tenant):
    one = await _profile(session, tenant, "A")
    two = await _profile(session, tenant, "B")
    with pytest.raises(identity.IdentityError, match="reason"):
        await identity.merge(session, tenant_uuid=tenant.id, winner_id=one.id,
                             loser_id=two.id, reason="dup", merged_by="alice")


async def test_an_undo_puts_back_exactly_what_the_merge_took(session, tenant):
    """Not "everything that looks like it came from there" — a profile that
    gained identifiers after the merge keeps them."""
    one = await _profile(session, tenant, "A")
    two = await _profile(session, tenant, "B")
    moved = await identity.add_identifier(
        session, tenant_uuid=tenant.id, customer_id=two.id, kind="email",
        value="b@example.test")
    await session.commit()
    record = await identity.merge(session, tenant_uuid=tenant.id, winner_id=one.id,
                                  loser_id=two.id, reason="looked like a duplicate",
                                  merged_by="alice")
    await session.commit()
    later = await identity.add_identifier(
        session, tenant_uuid=tenant.id, customer_id=one.id, kind="phone",
        value="+447700900001")
    await session.commit()

    await identity.unmerge(session, record=record, actor="alice")
    await session.commit()
    assert moved.customer_id == two.id     # went back
    assert later.customer_id == one.id     # stayed
    assert two.status == "active"


# ══ "to what extent" — the answer ═══════════════════════════════════════════


async def test_anonymous_shows_an_initial_and_a_count_and_nothing_else(session, tenant):
    row = await _profile(session, tenant, "Jane Smith")
    out = await three_sixty.compose(session, profile=row, tier="none",
                                    allowed=ALL_GRANTS)
    assert set(out["panels"]) == {"summary"}
    summary = out["panels"]["summary"]
    assert summary["name_initial"] == "J"
    assert summary["display_name"] is None
    # Everything else is named as withheld, not silently missing.
    withheld = {w["panel"] for w in out["withheld"]}
    assert "cases" in withheld and "identifiers" in withheld


async def test_partial_shows_cases_but_not_identifiers(session, tenant):
    row = await _profile(session, tenant)
    out = await three_sixty.compose(session, profile=row, tier="partial",
                                    allowed=ALL_GRANTS)
    assert "cases" in out["panels"] and "identifiers" not in out["panels"]


async def test_the_tier_can_only_narrow_what_hxguard_permits(session, tenant):
    """THE rule. A 360 must never become an authorisation bypass, and it would
    be one the moment a panel fetched on the composer's authority."""
    row = await _profile(session, tenant)
    grants = {**ALL_GRANTS, "case.read": False}
    out = await three_sixty.compose(session, profile=row, tier="full",
                                    allowed=grants)
    assert "cases" not in out["panels"]
    reason = next(w["why"] for w in out["withheld"] if w["panel"] == "cases")
    assert "not permitted" in reason


async def test_a_panel_the_rep_may_not_read_is_absent_not_empty(session, tenant):
    """An empty panel says "this customer has none of these"; an absent one says
    "this is not yours to see". A rep will act on the first."""
    row = await _profile(session, tenant)
    grants = {**ALL_GRANTS, "channels.conversations.read": False}
    out = await three_sixty.compose(session, profile=row, tier="full",
                                    allowed=grants)
    assert "conversations" not in out["panels"]
    assert out["panels"].get("conversations") != []


async def test_the_two_reasons_for_withholding_are_distinguished(session, tenant):
    """"You may not see this" and "verify the caller first" send a rep to
    different actions."""
    row = await _profile(session, tenant)
    grants = {**ALL_GRANTS, "case.read": False}
    out = await three_sixty.compose(session, profile=row, tier="none", allowed=grants)
    reasons = {w["panel"]: w["why"] for w in out["withheld"]}
    assert "not permitted" in reasons["cases"]
    assert "higher verification tier" in reasons["interactions"]


async def test_identifiers_are_masked_at_full_until_revealed(session, tenant):
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="phone",
                                  value="+447700900000")
    await session.commit()
    out = await three_sixty.compose(session, profile=row, tier="full",
                                    allowed=ALL_GRANTS)
    ident = out["panels"]["identifiers"][0]
    assert ident["masked"] is True
    assert "900000" not in ident["value"] and ident["value"].endswith("000")


async def test_a_revealed_field_is_unmasked_only_for_that_reveal(session, tenant):
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="phone",
                                  value="+447700900000")
    await session.commit()
    out = await three_sixty.compose(session, profile=row, tier="full",
                                    allowed=ALL_GRANTS,
                                    revealed={"identifiers.phone"})
    assert out["panels"]["identifiers"][0]["masked"] is False


async def test_a_reveal_below_full_is_refused(session, tenant):
    row = await _profile(session, tenant)
    with pytest.raises(identity.IdentityError, match="Full verification"):
        await three_sixty.reveal(session, tenant_uuid=tenant.id, profile=row,
                                 field_path="identifiers.phone",
                                 reason="customer asked", tier="partial",
                                 actor="alice")


async def test_a_reveal_needs_a_reason_and_leaves_a_record(session, tenant):
    row = await _profile(session, tenant)
    with pytest.raises(identity.IdentityError, match="reason"):
        await three_sixty.reveal(session, tenant_uuid=tenant.id, profile=row,
                                 field_path="identifiers.phone", reason="x",
                                 tier="full", actor="alice")
    await three_sixty.reveal(session, tenant_uuid=tenant.id, profile=row,
                             field_path="identifiers.phone",
                             reason="customer read it back to confirm",
                             tier="full", actor="alice")
    await session.commit()
    record = (await session.execute(select(CustomerDisclosureModel))).scalars().first()
    assert record.field_path == "identifiers.phone"
    # The tier is FROZEN — reading it live later would report what the call
    # eventually reached, not what it had reached when this was shown.
    assert record.at_verification == "full"


async def test_revealing_something_that_was_never_masked_is_refused(session, tenant):
    """Answering it would teach the caller that reveal is a way to fetch."""
    row = await _profile(session, tenant)
    with pytest.raises(identity.IdentityError, match="nothing to reveal"):
        await three_sixty.reveal(session, tenant_uuid=tenant.id, profile=row,
                                 field_path="summary.open_cases",
                                 reason="because I would like it",
                                 tier="full", actor="alice")


# ══ the call ════════════════════════════════════════════════════════════════


async def test_an_unresolved_call_is_still_an_interaction(session, tenant):
    """A contact centre that only records the calls it could identify has no
    idea how many it could not."""
    out = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+447700111222", agent_user_id="rep1")
    await session.commit()
    assert out["customer_id"] is None
    assert out["stub"]["actions"] == ["link_to_customer", "create_profile"]
    row = await session.get(CustomerInteractionModel, uuid.UUID(out["interaction_id"]))
    assert row is not None and row.verification_level == "none"


async def test_a_screen_pop_is_a_lookup_and_pops_the_person(session, tenant):
    row = await _profile(session, tenant, "Jane Smith")
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="phone",
                                  value="+447700900000", verified=True)
    await session.commit()
    out = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+44 7700 900000", agent_user_id="rep1")
    await session.commit()
    assert out["customer_id"] == str(row.id)
    assert out["resolution"]["rung"] == 1
    link = (await session.execute(
        select(CustomerLinkModel).where(
            CustomerLinkModel.object_kind == "interaction"))).scalars().first()
    assert link is not None


async def test_a_failed_verification_is_not_an_oracle(session, tenant):
    """One message whichever factor was wrong — an error that distinguishes them
    is a machine for guessing the rest."""
    out = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+447700900000")
    row = await session.get(CustomerInteractionModel, uuid.UUID(out["interaction_id"]))
    for factor in ("otp", "document", "call"):
        answer = await interactions.verify(session, interaction=row, factor=factor,
                                           ok=False, to_tier="full")
        assert answer["message"] == interactions.VERIFY_FAILED
        assert answer["verification_level"] == "none"


async def test_verification_lives_on_the_call_not_the_person(session, tenant):
    """A customer verified yesterday is not verified today."""
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="phone",
                                  value="+447700900000")
    await session.commit()
    first = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+447700900000")
    one = await session.get(CustomerInteractionModel, uuid.UUID(first["interaction_id"]))
    await interactions.verify(session, interaction=one, factor="otp", ok=True,
                              to_tier="full")
    await session.commit()

    second = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+447700900000")
    await session.commit()
    assert second["verification_level"] == "none"


async def test_a_full_verification_confirms_the_identifier_it_was_found_by(session, tenant):
    """The adoption at resolution time was a suggestion; this is the
    confirmation it was waiting for."""
    row = await _profile(session, tenant)
    ident = await identity.add_identifier(
        session, tenant_uuid=tenant.id, customer_id=row.id, kind="phone",
        value="+447700900000", source="resolution")
    await session.commit()
    assert ident.verified is False
    pop = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default",
        channel="voice", ani="+447700900000")
    call = await session.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
    await interactions.verify(session, interaction=call, factor="otp", ok=True,
                              to_tier="full")
    await session.commit()
    assert ident.verified is True and ident.verified_by == "otp"


async def test_a_warm_transfer_carries_the_tier(session, tenant):
    """Being re-verified is the number one complaint about every contact
    centre, and it is entirely within our power not to do it."""
    row = await _profile(session, tenant)
    pop = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default", channel="voice",
        ani=None, agent_user_id="rep1")
    call = await session.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
    call.customer_id = row.id
    await interactions.verify(session, interaction=call, factor="otp", ok=True,
                              to_tier="full")
    await session.commit()

    second = await interactions.transfer(session, tenant_uuid=tenant.id,
                                         interaction=call, to_agent="rep2")
    await session.commit()
    assert second.verification_level == "full"
    assert second.transferred_from == call.id
    assert second.customer_id == row.id
    # Two reps handled this contact, and the record says so.
    assert call.status == "wrapped" and second.status == "open"


async def test_an_unclosed_interaction_ages_out_and_blocks_nothing(session, tenant):
    """A wrap-up that blocks the next call is a wrap-up that gets faked."""
    pop = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default", channel="voice")
    call = await session.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
    call.started_at = datetime.now(timezone.utc) - timedelta(hours=24)
    await session.commit()
    assert await interactions.age_out(session, tenant_uuid=tenant.id) == 1
    assert call.status == "abandoned"


# ══ raising on the customer's behalf ════════════════════════════════════════


async def test_attribution_names_the_rep_and_stamps_the_tier(session, tenant):
    """`created_by` is the rep — they are accountable for the record — and the
    tier is frozen at creation so a later reader knows whether the person was
    ever proven."""
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="email",
                                  value="jane@example.test", is_primary=True)
    pop = await interactions.screen_pop(
        session, tenant_uuid=tenant.id, tenant_token="default", channel="voice")
    call = await session.get(CustomerInteractionModel, uuid.UUID(pop["interaction_id"]))
    await interactions.verify(session, interaction=call, factor="otp", ok=True,
                              to_tier="partial")
    await session.commit()

    out = await interactions.on_behalf_attribution(
        session, profile=row, interaction=call, rep_user_id="rep1")
    assert out["created_by"] == "rep1"
    assert out["on_behalf_of_customer_id"] == str(row.id)
    assert out["verification_level"] == "partial"
    assert out["raised_by_agent"] is True


async def test_missing_consent_offers_capture_not_a_refusal(session, tenant):
    """Decision 4: a hard refusal is correct and useless on a live call — the
    rep is talking to the very person whose consent it is."""
    row = await _profile(session, tenant)
    await identity.add_identifier(session, tenant_uuid=tenant.id,
                                  customer_id=row.id, kind="email",
                                  value="jane@example.test")
    await session.commit()
    out = await interactions.consent_gate(
        session, tenant_token="default", profile=row, purpose="marketing")
    assert out["granted"] is False
    assert out["action"] == "capture_consent_now"
    # And capturing is an ACT with a record, not a checkbox the rep ticks.
    assert "may not simply assert it" in out["note"]


# ══ the API ═════════════════════════════════════════════════════════════════


async def test_the_api_is_gated(anon_client):
    resp = await anon_client.get("/api/v1/engage/status")
    assert resp.status_code in (401, 403)
    resp = await anon_client.post("/api/v1/engage/profiles", json={})
    assert resp.status_code in (401, 403, 422)


async def test_a_dark_tenant_gets_a_404_not_a_403(client, tenant):
    """A tenant without the entitlement should not learn the feature exists."""
    resp = await client.post("/api/v1/engage/profiles",
                             json={"display_name": "Jane"})
    assert resp.status_code == 404
