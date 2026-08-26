"""Schedules — the review gate, the claim, and what an unattended action may do.

A scheduler makes things happen with nobody watching and nobody holding the
authority for them, so what these tests are about is mostly refusals: who cannot
approve, what cannot be edited without re-approval, what cannot be reached across
a tenant boundary, and what an unattended run is not allowed to change.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.bots import service as bot_service
from velaris_shared.db.models import (
    BotDefinitionModel,
    CaseInstanceModel,
    CaseTypeModel,
    ScheduleModel,
    ScheduleRunModel,
)
from case_service.scheduler import actions, service

BASE = "/api/v1/scheduler"
UTC = timezone.utc
DIGEST = "sha256:" + "a" * 64


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _schedule(session, **overrides) -> ScheduleModel:
    row = ScheduleModel(
        tenant_id=overrides.pop("tenant_id", "default"),
        name=overrides.pop("name", _uniq("sched")),
        description="", cron=overrides.pop("cron", "0 2 * * *"),
        timezone=overrides.pop("timezone", "UTC"),
        action_kind=overrides.pop("action_kind", "sync"),
        action_config=overrides.pop("action_config", {"graph": True}),
        status=overrides.pop("status", "draft"),
        created_by=overrides.pop("created_by", "author"),
        **overrides)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _bot(session, **overrides) -> BotDefinitionModel:
    row = BotDefinitionModel(
        tenant_id=overrides.pop("tenant_id", "default"), name=_uniq("bot"),
        description="", image="registry.test/bot", digest=DIGEST,
        declared_command=[], declared_env={}, credential_refs=[],
        input_schema={}, output_schema={}, granted_domains=[],
        status=overrides.pop("status", "active"), created_by="author",
        **overrides)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ── the review gate ───────────────────────────────────────────────────────

class TestApproval:
    @pytest.mark.asyncio
    async def test_a_new_schedule_is_draft_and_has_no_firing_time(self, session):
        row = await _schedule(session)
        assert row.status == "draft" and row.next_run_at is None

    @pytest.mark.asyncio
    async def test_the_author_cannot_approve_their_own(self, session):
        """Approval *is* the review. Reviewing your own unattended 2am job is
        not a review."""
        row = await _schedule(session, created_by="sam")
        with pytest.raises(service.ScheduleError, match="somebody other"):
            await service.approve(session, row, approver="Sam")   # case-insensitive

    @pytest.mark.asyncio
    async def test_approval_starts_the_clock(self, session):
        row = await _schedule(session, created_by="sam")
        await service.approve(session, row, approver="alex")
        assert row.status == "active"
        assert row.approved_by == "alex" and row.next_run_at is not None

    @pytest.mark.asyncio
    async def test_a_draft_cannot_be_run_by_hand(self, session):
        """'Run now' on an unreviewed action would be the approval gate with an
        extra step."""
        row = await _schedule(session)
        with pytest.raises(service.ScheduleError, match="not been reviewed"):
            await service.run_now(session, row, actor="admin")

    @pytest.mark.asyncio
    async def test_disabling_clears_the_firing_time(self, session):
        row = await _schedule(session, created_by="sam")
        await service.approve(session, row, approver="alex")
        await service.disable(session, row)
        assert row.status == "disabled" and row.next_run_at is None


class TestApprovalOverHttp:
    @pytest.mark.asyncio
    async def test_an_edit_returns_an_approved_schedule_to_draft(self, client, session):
        created = (await client.post(BASE, json={
            "name": _uniq("nightly"), "cron": "0 2 * * *",
            "action_kind": "sync", "action_config": {"graph": True},
        })).json()
        row = await session.get(ScheduleModel, uuid.UUID(created["id"]))
        row.status, row.approved_by = "active", "someone-else"
        row.next_run_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()

        edited = await client.put(f"{BASE}/{created['id']}", json={
            "name": created["name"], "cron": "0 3 * * *",
            "action_kind": "sync", "action_config": {"graph": True},
        })
        assert edited.status_code == 200
        body = edited.json()
        assert body["status"] == "draft"
        assert body["approved_by"] is None
        # Cleared, or the loop would claim a firing nobody re-approved.
        assert body["next_run_at"] is None

    @pytest.mark.asyncio
    async def test_a_no_op_save_does_not_de_approve(self, client, session):
        """Otherwise opening a schedule and pressing save is a denial of service
        on anything anyone happened to look at."""
        created = (await client.post(BASE, json={
            "name": _uniq("nightly"), "cron": "0 2 * * *",
            "action_kind": "sync", "action_config": {"graph": True},
        })).json()
        row = await session.get(ScheduleModel, uuid.UUID(created["id"]))
        row.status, row.approved_by = "active", "someone-else"
        await session.commit()

        again = await client.put(f"{BASE}/{created['id']}", json={
            "name": created["name"], "description": "", "cron": "0 2 * * *",
            "timezone": "UTC", "action_kind": "sync",
            "action_config": {"graph": True},
        })
        assert again.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_an_expression_that_never_fires_is_refused_on_write(self, client):
        r = await client.post(BASE, json={
            "name": _uniq("never"), "cron": "0 0 30 2 *",
            "action_kind": "sync", "action_config": {"graph": True},
        })
        assert r.status_code == 400 and "no firing time" in r.text

    @pytest.mark.asyncio
    async def test_the_preview_shows_the_next_times_before_anyone_saves(self, client):
        r = await client.post(f"{BASE}/preview", json={
            "cron": "0 2 * * *", "timezone": "Europe/Berlin", "count": 3})
        assert r.status_code == 200 and len(r.json()["next"]) == 3

    @pytest.mark.asyncio
    async def test_the_literal_paths_are_not_swallowed_by_the_id_route(self, client):
        """`/actions` and `/preview` are words, not ids. Declared after
        `/{schedule_id}` they would answer 422 on a UUID nobody sent."""
        assert (await client.get(f"{BASE}/actions")).status_code == 200

    @pytest.mark.asyncio
    async def test_every_offered_action_exists(self, client):
        offered = {a["kind"] for a in (await client.get(f"{BASE}/actions")).json()["actions"]}
        assert offered == set(actions.kinds())

    @pytest.mark.asyncio
    async def test_anonymous_callers_are_refused(self, anon_client):
        for r, label in (
            (await anon_client.get(BASE), "GET /scheduler"),
            (await anon_client.post(BASE, json={}), "POST /scheduler"),
            (await anon_client.get(f"{BASE}/actions"), "GET /actions"),
            (await anon_client.post(f"{BASE}/preview", json={"cron": "0 2 * * *"}),
             "POST /preview"),
        ):
            assert r.status_code in (401, 403), f"{label} -> {r.status_code}"


# ── the claim ─────────────────────────────────────────────────────────────

class TestClaiming:
    @pytest.mark.asyncio
    async def test_a_due_schedule_is_claimed_once(self, session):
        """Two workers racing for one firing: the conditional UPDATE names the
        `next_run_at` that was read, so the loser's rowcount is zero."""
        row = await _schedule(session, created_by="sam", cron="*/5 * * * *")
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=30)
        await session.commit()

        first = await service.claim_due(session)
        await session.commit()
        second = await service.claim_due(session)
        assert len(first) == 1 and second == []

    @pytest.mark.asyncio
    async def test_the_clock_moves_before_the_action_runs(self, session):
        row = await _schedule(session, created_by="sam", cron="*/5 * * * *")
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=30)
        await session.commit()

        claimed = await service.claim_due(session)
        # SQLite hands back naive datetimes; the column is UTC either way.
        moved = claimed[0][0].next_run_at
        moved = moved if moved.tzinfo else moved.replace(tzinfo=UTC)
        assert moved > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_a_missed_window_is_skipped_not_replayed(self, session):
        """The platform was down overnight. Breakfast must not bring a night's
        worth of email."""
        row = await _schedule(session, created_by="sam", cron="0 2 * * *")
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(hours=6)
        await session.commit()

        claimed = await service.claim_due(session)
        assert len(claimed) == 1 and claimed[0][1] is True

    @pytest.mark.asyncio
    async def test_a_draft_is_never_claimed(self, session):
        row = await _schedule(session)
        row.next_run_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()
        assert await service.claim_due(session) == []

    @pytest.mark.asyncio
    async def test_a_cron_that_stopped_resolving_parks_itself(self, session):
        """A stored expression that can no longer fire must not spin the loop."""
        row = await _schedule(session, created_by="sam")
        await service.approve(session, row, approver="alex")
        row.cron = "0 0 30 2 *"
        row.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

        assert await service.claim_due(session) == []
        await session.refresh(row)
        assert row.status == "disabled" and row.last_status == "error"


# ── what an action may do ─────────────────────────────────────────────────

class TestActionBounds:
    @pytest.mark.asyncio
    async def test_a_schedule_cannot_fire_another_tenants_bot(self, session):
        theirs = await _bot(session, tenant_id="other-tenant")
        row = await _schedule(session, action_kind="bot",
                              action_config={"bot_id": str(theirs.id)})
        with pytest.raises(actions.ScheduleActionError, match="not found"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_an_approved_schedule_cannot_run_a_draft_bot(self, session):
        """The scheduler is not a second way through the bot approval gate."""
        draft = await _bot(session, status="draft")
        row = await _schedule(session, created_by="sam", action_kind="bot",
                              action_config={"bot_id": str(draft.id)})
        await service.approve(session, row, approver="alex")
        await session.commit()

        result = await service.run_now(session, row, actor="alex")
        assert result["status"] == "error"
        assert "not been reviewed" in result["detail"]["error"]

    @pytest.mark.asyncio
    async def test_a_scheduled_bot_run_queues_a_job(self, session):
        bot = await _bot(session)
        row = await _schedule(session, created_by="sam", action_kind="bot",
                              action_config={"bot_id": str(bot.id), "input": {"a": 1}})
        await service.approve(session, row, approver="alex")
        result = await service.run_now(session, row, actor="alex")
        await session.commit()

        assert result["status"] == "ok" and len(result["detail"]["jobs"]) == 1
        job = await session.get(
            __import__("velaris_shared.db.models", fromlist=["BotJobModel"]).BotJobModel,
            uuid.UUID(result["detail"]["jobs"][0]))
        assert job.trigger == "schedule"

    @pytest.mark.asyncio
    async def test_a_bulk_action_cannot_move_a_case_status(self, session):
        """A transition is the workflow's decision; driving it from a cron line
        routes around the process the customer designed."""
        row = await _schedule(session, action_kind="case_bulk", action_config={
            "match": {"status": "new"}, "apply": {"status": "closed"}})
        with pytest.raises(actions.ScheduleActionError, match="belongs to the workflow"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_a_bulk_action_cannot_assign(self, session):
        row = await _schedule(session, action_kind="case_bulk", action_config={
            "match": {}, "apply": {"assign_to": "sam"}})
        with pytest.raises(actions.ScheduleActionError, match="needs a step"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_a_bulk_action_refuses_rather_than_truncates(self, session):
        """Quietly doing the first N of many and reporting success is the worse
        answer."""
        case_type = CaseTypeModel(
            name=_uniq("ct"), version=1, definition_json={}, tenant_id=None)
        session.add(case_type)
        await session.flush()
        for _ in range(4):
            session.add(CaseInstanceModel(
                case_type_id=case_type.id, case_type_version=1, status="new",
                priority="normal", data={}, tenant_id=None))
        await session.commit()

        row = await _schedule(session, action_kind="case_bulk", action_config={
            "match": {"status": "new", "case_type_id": str(case_type.id)},
            "apply": {"set_variables": {"chased": True}}, "max_cases": 2})
        with pytest.raises(actions.ScheduleActionError, match="more than 2"):
            await actions.get("case_bulk").run(session, row, row.action_config)

    @pytest.mark.asyncio
    async def test_a_bulk_action_sets_variables_but_not_underscore_keys(self, session):
        case_type = CaseTypeModel(
            name=_uniq("ct"), version=1, definition_json={}, tenant_id=None)
        session.add(case_type)
        await session.flush()
        case = CaseInstanceModel(
            case_type_id=case_type.id, case_type_version=1, status="new",
            priority="normal", data={"keep": 1}, tenant_id=None)
        session.add(case)
        await session.commit()

        row = await _schedule(session, action_kind="case_bulk", action_config={
            "match": {"case_type_id": str(case_type.id)},
            "apply": {"set_variables": {"chased": True, "_internal": "no"},
                      "priority": "high"}, "max_cases": 10})
        detail = await actions.get("case_bulk").run(session, row, row.action_config)
        await session.commit()
        await session.refresh(case)

        assert detail["changed"] == 1
        assert case.data["chased"] is True and case.data["keep"] == 1
        assert "_internal" not in case.data
        assert case.priority == "high"

    @pytest.mark.asyncio
    async def test_a_scheduled_send_is_capped(self, session):
        row = await _schedule(session, action_kind="email", action_config={
            "to": [f"p{i}@test.invalid" for i in range(101)], "subject": "hi"})
        with pytest.raises(actions.ScheduleActionError, match="capped at 100"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_an_email_needs_a_subject_or_a_template(self, session):
        from velaris_shared.db.models import EmailAccountModel

        account = EmailAccountModel(
            name="out", address="out@test.invalid", smtp_host="smtp.test.invalid",
            is_active=True, is_default_outbound=True)
        session.add(account)
        await session.commit()

        row = await _schedule(session, action_kind="email",
                              action_config={"to": ["a@test.invalid"]})
        with pytest.raises(actions.ScheduleActionError, match="subject"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused_on_write(self, session):
        row = await _schedule(session, action_kind="rm_-rf", action_config={})
        with pytest.raises(service.ScheduleError, match="Unknown action"):
            await service.validate_schedule(session, row)

    @pytest.mark.asyncio
    async def test_a_sync_schedule_must_say_what_to_sync(self, session):
        row = await _schedule(session, action_kind="sync", action_config={})
        with pytest.raises(actions.ScheduleActionError, match="Say what to sync"):
            await service.validate_schedule(session, row)


# ── the record ────────────────────────────────────────────────────────────

class TestRunHistory:
    @pytest.mark.asyncio
    async def test_a_failing_run_is_recorded_not_raised(self, session):
        """A schedule is a standing instruction: one bad night must not stop the
        timetable, and the failure has to be answerable from a row."""
        bot = await _bot(session, status="draft")
        row = await _schedule(session, created_by="sam", action_kind="bot",
                              action_config={"bot_id": str(bot.id)})
        await service.approve(session, row, approver="alex")
        await service.run_now(session, row, actor="alex")
        await session.commit()

        from sqlalchemy import select

        runs = (await session.execute(
            select(ScheduleRunModel).where(ScheduleRunModel.schedule_id == row.id)
        )).scalars().all()
        assert len(runs) == 1 and runs[0].status == "error"
        await session.refresh(row)
        assert row.last_status == "error" and row.last_error

    @pytest.mark.asyncio
    async def test_a_manual_run_does_not_move_the_timetable(self, session):
        """Testing a schedule must not silently skip tonight's run."""
        row = await _schedule(session, created_by="sam", action_kind="sync",
                              action_config={"graph": True})
        await service.approve(session, row, approver="alex")
        before = row.next_run_at
        await service.run_now(session, row, actor="alex")
        assert row.next_run_at == before


# ── the loop ──────────────────────────────────────────────────────────────

class TestLoop:
    """The loop is what makes any of this happen without a person. These run its
    real `tick()`, not a stand-in for it."""

    @pytest.mark.asyncio
    async def test_a_tick_fires_a_due_schedule_and_records_it(self, session, monkeypatch):
        from case_service.scheduler.runner import loop

        bot = await _bot(session)
        row = await _schedule(session, created_by="sam", action_kind="bot",
                              action_config={"bot_id": str(bot.id)})
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=10)
        await session.commit()

        fired = await loop.tick()
        assert fired == 1

        from sqlalchemy import select

        runs = (await session.execute(
            select(ScheduleRunModel).where(ScheduleRunModel.schedule_id == row.id)
        )).scalars().all()
        assert len(runs) == 1 and runs[0].status == "ok"

    @pytest.mark.asyncio
    async def test_a_second_tick_does_not_fire_it_again(self, session):
        from case_service.scheduler.runner import loop

        row = await _schedule(session, created_by="sam", cron="0 2 * * *",
                              action_kind="sync", action_config={"graph": True})
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=10)
        await session.commit()

        assert await loop.tick() == 1
        assert await loop.tick() == 0

    @pytest.mark.asyncio
    async def test_a_missed_window_is_recorded_as_skipped(self, session):
        from case_service.scheduler.runner import loop

        row = await _schedule(session, created_by="sam", action_kind="sync",
                              action_config={"graph": True})
        await service.approve(session, row, approver="alex")
        row.next_run_at = datetime.now(UTC) - timedelta(hours=8)
        await session.commit()

        await loop.tick()

        from sqlalchemy import select

        runs = (await session.execute(
            select(ScheduleRunModel).where(ScheduleRunModel.schedule_id == row.id)
        )).scalars().all()
        assert len(runs) == 1 and runs[0].status == "skipped"
        assert "missed" in runs[0].detail["reason"]
