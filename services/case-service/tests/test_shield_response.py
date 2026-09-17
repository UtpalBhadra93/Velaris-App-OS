"""HxShield's response layer — and the four ways it declines to act.

The engine has computed an action for every incident since it shipped and
nothing ever performed one. Adding the half that acts is easy; the tests that
matter are the ones proving it does not act when it should not:

* off by default — an install that takes this update behaves identically;
* a destructive response waits for a person, whatever the switch says;
* a burst does not become a queue;
* a failure is a recorded state, never an exception thrown at a login.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.hxshield import response as R
from velaris_shared.db.models import ShieldResponseModel


@pytest.fixture
def responses_on(monkeypatch):
    monkeypatch.setattr(R, "responses_enabled", lambda: True)


async def _propose(session, **overrides):
    kwargs = dict(
        action="flag",
        incident_id=uuid.uuid4(),
        actor_id="alice",
        tenant_id=None,
        reason="two failed logins then an export",
        patterns=["off_hours_bulk_access"],
        score=0.85,
    )
    kwargs.update(overrides)
    return await R.propose(session, **kwargs)


# ── Off by default ───────────────────────────────────────────────────────────


class TestTheSwitch:
    async def test_with_responses_off_nothing_is_performed(self, session):
        """An install that takes this update behaves exactly as it did."""
        assert R.responses_enabled() is False, "the shipped default is OFF"
        row = await _propose(session)
        assert row.state == "shadowed"
        assert row.executed_at is None
        assert "switched off" in row.result

    async def test_a_shadowed_row_says_why_rather_than_looking_forgotten(self, session):
        """A queue full of shadowed rows is the platform working as configured,
        not a backlog somebody ignored — and it has to be possible to tell those
        apart at a glance."""
        row = await _propose(session)
        assert "VELARIS_CASE_SHIELD_RESPONSE_ENABLED" in row.result

    async def test_a_shadowed_row_cannot_be_approved_into_action(self, session):
        row = await _propose(session)
        with pytest.raises(PermissionError):
            await R.approve(session, row, actor="admin")

    async def test_with_responses_on_a_reversible_one_runs(self, session, responses_on):
        row = await _propose(session, action="alert")
        assert row.state == "executed"
        assert row.destructive is False


# ── The approval gate ────────────────────────────────────────────────────────


class TestDestructiveAlwaysWaits:
    @pytest.mark.parametrize("action,expected", [
        ("suspend", "suspend_user"),
        ("block", "revoke_sessions"),
    ])
    async def test_a_destructive_response_is_proposed_not_executed(
            self, session, responses_on, action, expected):
        """Even with the switch ON. The switch governs the reversible ones."""
        row = await _propose(session, action=action)
        assert row.response_type == expected
        assert row.destructive is True
        assert row.state == "proposed"
        assert row.executed_at is None

    async def test_execute_refuses_a_row_nobody_approved(self, session, responses_on):
        """The guard is at the point of change, not only at the route — a
        response is the one thing here that changes the world."""
        row = await _propose(session, action="suspend")
        with pytest.raises(PermissionError):
            await R.execute(session, row, actor="admin")

    async def test_approval_records_who_and_when(self, session, responses_on):
        row = await _propose(session, action="suspend", actor_id="nobody-in-particular")
        await R.approve(session, row, actor="secops@example.com")
        assert row.approved_by == "secops@example.com"
        assert row.approved_at is not None
        assert row.state in ("executed", "failed")

    async def test_declining_is_recorded_as_firmly_as_approving(self, session, responses_on):
        """A rule declined every time is a rule that should change, and that
        only becomes visible if 'no' is written down."""
        row = await _propose(session, action="suspend")
        await R.decline(session, row, actor="secops", reason="known pen test")
        assert row.state == "declined"
        assert row.declined_by == "secops"
        assert "known pen test" in row.result

    async def test_an_executed_response_cannot_be_declined_afterwards(
            self, session, responses_on):
        row = await _propose(session, action="alert")
        assert row.state == "executed"
        with pytest.raises(PermissionError):
            await R.decline(session, row, actor="secops")


# ── Not turning a burst into a queue ─────────────────────────────────────────


class TestRestraint:
    async def test_the_same_target_is_not_proposed_twice_in_the_window(
            self, session, responses_on):
        first = await _propose(session, action="suspend", actor_id="alice")
        second = await _propose(session, action="suspend", actor_id="alice")
        assert first is not None
        assert second is None, (
            "a hundred events must not queue a hundred identical suspensions")

    async def test_a_different_target_is_still_proposed(self, session, responses_on):
        await _propose(session, action="suspend", actor_id="alice")
        assert await _propose(session, action="suspend", actor_id="bob") is not None

    async def test_the_window_expires(self, session, responses_on):
        first = await _propose(session, action="suspend", actor_id="carol")
        first.created_at = datetime.now(timezone.utc) - R.DEDUPE_WINDOW - timedelta(minutes=1)
        await session.flush()
        assert await _propose(session, action="suspend", actor_id="carol") is not None, (
            "a genuine second incident tomorrow is its own decision")

    async def test_an_anonymous_actor_is_not_suspended(self, session, responses_on):
        """There is nothing to act on, and inventing a target is how the wrong
        account gets disabled."""
        assert await _propose(session, action="suspend", actor_id=None) is None

    async def test_an_action_with_no_mapping_proposes_nothing(self, session, responses_on):
        """The vocabulary is closed. A response type this platform has never
        heard of is unrepresentable, not merely unrecognised."""
        assert await _propose(session, action="launch_the_missiles") is None

    async def test_propose_never_raises_into_its_caller(self, session, monkeypatch):
        """It runs from the detection hook, which runs from inside
        log_security_event, which runs from inside login."""
        monkeypatch.setattr(R, "responses_enabled",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert await _propose(session) is None


# ── The primitives ───────────────────────────────────────────────────────────


class TestPrimitives:
    async def test_every_registered_response_has_a_primitive(self):
        """A response in the catalogue with nothing behind it is a queue entry
        that approves into silence."""
        assert set(R.RESPONSES) == set(R._PRIMITIVES)

    async def test_suspending_an_unknown_account_fails_loudly_on_the_row(
            self, session, responses_on):
        """A response that silently did nothing is the failure mode that
        matters — the queue would show 'executed' over an account still able to
        log in."""
        row = await _propose(session, action="suspend", actor_id="ghost@example.com")
        await R.approve(session, row, actor="secops")
        assert row.state == "failed"
        assert "ghost@example.com" in (row.result or "")

    async def test_suspend_disables_both_the_directory_and_the_login_table(
            self, session, responses_on):
        """Either alone leaves an account disabled on one screen and still able
        to sign in."""
        from velaris_shared.db.models import HelixUserModel, UserDirectoryModel

        session.add(UserDirectoryModel(
            user_id="dave", email="dave@example.com", display_name="Dave",
            is_active=True))
        session.add(HelixUserModel(
            id=uuid.uuid4(), username="dave", email="dave@example.com",
            password_hash="x", is_active=True))
        await session.flush()

        row = await _propose(session, action="suspend", actor_id="dave")
        await R.approve(session, row, actor="secops")
        assert row.state == "executed", row.result

        from sqlalchemy import select
        directory = (await session.execute(
            select(UserDirectoryModel).where(UserDirectoryModel.user_id == "dave")
        )).scalars().first()
        auth = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == "dave")
        )).scalars().first()
        assert directory.is_active is False
        assert auth.is_active is False

    def test_the_catalogue_marks_the_destructive_ones_as_needing_approval(self):
        by_name = {r["name"]: r for r in R.catalogue()}
        assert by_name["suspend_user"]["needs_approval"] is True
        assert by_name["notify"]["needs_approval"] is False

    def test_block_says_what_it_actually_does(self):
        """This platform has no packet-level control to offer. A response named
        'block' that quietly did something weaker would be worse than one that
        names the weaker thing."""
        assert R.ACTION_TO_RESPONSE["block"] == "revoke_sessions"


# ── Through the API ──────────────────────────────────────────────────────────


class TestApi:
    async def test_the_queue_says_whether_responses_are_on(self, client):
        resp = await client.get("/api/v1/shield/responses")
        assert resp.status_code == 200
        assert resp.json()["responses_enabled"] is False
        assert {r["name"] for r in resp.json()["catalogue"]} == set(R.RESPONSES)

    async def test_approving_a_shadowed_response_is_refused_with_a_reason(
            self, client, session):
        row = await _propose(session)
        await session.commit()
        resp = await client.post(f"/api/v1/shield/responses/{row.id}/approve")
        assert resp.status_code == 409
        assert "switched off" in resp.json()["detail"]

    async def test_an_unknown_response_is_a_404(self, client):
        resp = await client.post(f"/api/v1/shield/responses/{uuid.uuid4()}/approve")
        assert resp.status_code == 404


class TestTheAuditFoundThese:
    """Found by asking "is it 100% wired and 100% secure?" — 2026-09-09."""

    def test_every_registered_response_is_REACHABLE_from_a_rule_action(self):
        """The stronger form of "has a primitive". Two responses were written
        and removed the same day: `quarantine_case`, whose marker in `case.data`
        **nothing read**, and `revoke_agent_tokens`, which no rule action mapped
        to — so it could sit in the catalogue forever and never be proposed.

        That is HxShield's own history repeating: eight patterns, one caller,
        zero incidents. A catalogue entry with nothing behind it is a promise
        that cannot be kept."""
        reachable = set(R.ACTION_TO_RESPONSE.values())
        assert set(R.RESPONSES) == reachable, (
            f"unreachable response(s): {set(R.RESPONSES) - reachable} — no rule "
            f"action maps to them, so they can never be proposed")

    def test_every_mapped_action_names_a_response_that_exists(self):
        """The other direction: a rule action pointing at nothing would raise a
        KeyError inside the detection hook, which runs inside login."""
        assert set(R.ACTION_TO_RESPONSE.values()) <= set(R.RESPONSES)

    async def test_one_tenants_admin_cannot_act_on_anothers_response(
            self, client, session):
        """The queue is not a report — its buttons suspend accounts. Without a
        tenant clause a tenant administrator could approve a destructive
        response against another tenant's user."""
        row = await _propose(session, tenant_id="some-other-tenant")
        await session.commit()
        assert row.tenant_id == "some-other-tenant"

        listed = await client.get("/api/v1/shield/responses")
        assert listed.status_code == 200
        assert str(row.id) not in [i["id"] for i in listed.json()["items"]]

        # 404, not 403: a 403 confirms the row exists, and it names an actor.
        assert (await client.post(
            f"/api/v1/shield/responses/{row.id}/approve")).status_code == 404
        assert (await client.post(
            f"/api/v1/shield/responses/{row.id}/decline")).status_code == 404

    async def test_an_unscoped_response_stays_visible(self, client, session):
        """Most rows have no tenant — a security event does not carry one — and
        scoping them away would empty the queue on every install."""
        row = await _propose(session, tenant_id=None)
        await session.commit()
        listed = await client.get("/api/v1/shield/responses")
        assert str(row.id) in [i["id"] for i in listed.json()["items"]]
