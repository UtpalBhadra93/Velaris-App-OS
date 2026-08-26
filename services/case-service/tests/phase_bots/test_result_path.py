"""RPA bots — the approval gate, the queue, and the result path.

A bot holds live credentials to a third party's production system and drives a
browser. It is *more* dangerous than a Layer-2 marketplace app, which only reads
tenant data through a scoped broker. So the tests here are about what a bot
cannot do: run unreviewed, spend its result token twice, write anything but case
variables, or hold a password and wildcard egress at the same time.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.bots import credentials, service, tokens
from velaris_shared.db.models import (
    BotCredentialLeaseModel,
    BotCredentialModel,
    BotDefinitionModel,
    BotJobModel,
)

BASE = "/api/v1/bots"
DIGEST = "sha256:" + "a" * 64


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _bot(session, *, status="active", **overrides) -> BotDefinitionModel:
    row = BotDefinitionModel(
        tenant_id=overrides.pop("tenant_id", "default"),
        name=overrides.pop("name", _uniq("bot")),
        description="", image=overrides.pop("image", "registry.test/bot"),
        digest=overrides.pop("digest", DIGEST),
        declared_command=[], declared_env={},
        credential_refs=overrides.pop("credential_refs", []),
        input_schema={}, output_schema=overrides.pop("output_schema", {}),
        granted_domains=overrides.pop("granted_domains", []),
        status=status, created_by=overrides.pop("created_by", "author"),
        **overrides,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _job(session, bot, **overrides) -> BotJobModel:
    job = BotJobModel(
        tenant_id=bot.tenant_id, bot_id=bot.id,
        trigger=overrides.pop("trigger", "api"),
        input={}, artifacts=[], status=overrides.pop("status", "running"),
        **overrides,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# ── the approval gate ─────────────────────────────────────────────────────

class TestApproval:
    @pytest.mark.asyncio
    async def test_a_draft_bot_cannot_be_run(self, session):
        """An unapproved bot has not been reviewed, and a container image is
        exactly the thing you review before it runs on your host."""
        bot = await _bot(session, status="draft")
        with pytest.raises(service.BotError):
            await service.enqueue(session, bot=bot, input_data={}, trigger="api")

    @pytest.mark.asyncio
    async def test_the_author_cannot_approve_their_own_bot(self, session):
        """Approval *is* the review. Reviewing your own container image is not
        one, and this is the same separation the Layer-2 grant flow enforces."""
        bot = await _bot(session, status="draft", created_by="ada")
        with pytest.raises(service.BotError) as caught:
            await service.approve(session, bot, approver="ada")
        assert caught.value.status == 409
        assert bot.status == "draft"

    @pytest.mark.asyncio
    async def test_somebody_else_can_approve(self, session):
        bot = await _bot(session, status="draft", created_by="ada")
        await service.approve(session, bot, approver="grace")
        assert bot.status == "active" and bot.approved_by == "grace"

    @pytest.mark.asyncio
    async def test_an_unpinned_image_is_refused(self, session):
        """A tag can be repointed at different content after review, which makes
        the approval meaningless."""
        bot = await _bot(session, status="draft", digest="latest")
        with pytest.raises(service.BotError):
            service.validate_definition(bot)

    @pytest.mark.asyncio
    async def test_credentials_plus_wildcard_egress_is_refused(self, session):
        """A live supplier password and permission to reach anywhere is an
        exfiltration tool. Each half is reasonable alone, which is exactly why
        the combination is checked in one place."""
        bot = await _bot(
            session, status="draft",
            credential_refs=["vault://tenant/acme/portal"],
            granted_domains=["*"])
        with pytest.raises(service.BotError) as caught:
            service.validate_definition(bot)
        assert "exfiltration" in str(caught.value)

    @pytest.mark.asyncio
    async def test_named_domains_with_credentials_are_fine(self, session):
        bot = await _bot(
            session, status="draft",
            credential_refs=["vault://tenant/acme/portal"],
            granted_domains=["supplier.example.com"])
        service.validate_definition(bot)


# ── the queue ─────────────────────────────────────────────────────────────

class TestQueue:
    @pytest.mark.asyncio
    async def test_leasing_is_atomic_between_two_runners(self, session):
        """The claim is a conditional UPDATE whose WHERE still names the old
        status, so the loser's rowcount is zero. That is what makes a second
        runner safe without a distributed lock."""
        bot = await _bot(session)
        await service.enqueue(session, bot=bot, input_data={}, trigger="api")
        await session.commit()

        first = await service.lease_next(session)
        second = await service.lease_next(session)
        await session.commit()

        assert first is not None
        assert second is None or second.id != first.id

    @pytest.mark.asyncio
    async def test_an_expired_lease_comes_back(self, session):
        """A crashed runner must not lose the job."""
        from datetime import datetime, timedelta, timezone

        bot = await _bot(session)
        job = await service.enqueue(session, bot=bot, input_data={}, trigger="api")
        await session.commit()

        leased = await service.lease_next(session)
        leased.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await session.commit()

        again = await service.lease_next(session)
        await session.commit()
        assert again is not None and again.id == job.id
        assert again.attempt == 2

    @pytest.mark.asyncio
    async def test_max_concurrency_is_respected(self, session):
        bot = await _bot(session, max_concurrency=1)
        await _job(session, bot, status="running")
        assert await service.at_concurrency_limit(session, bot) is True

    @pytest.mark.asyncio
    async def test_failures_retry_with_the_connector_backoff(self, session):
        """One answer to "how does an integration failure behave here", not two."""
        bot = await _bot(session)
        job = await _job(session, bot, attempt=1)
        outcome = await service.requeue_or_fail(session, job, error="boom")
        assert outcome == "requeued" and job.status == "queued"

        job.attempt = service.MAX_ATTEMPTS
        outcome = await service.requeue_or_fail(session, job, error="boom")
        assert outcome == "failed" and job.status == "failed"


# ── the result token ──────────────────────────────────────────────────────

class TestResultToken:
    @pytest.mark.asyncio
    async def test_the_right_token_is_accepted_once(self, session, anon_client):
        bot = await _bot(session)
        job = await _job(session, bot)
        token = await service.mint_result_token(session, job)
        await session.commit()

        first = await anon_client.post(
            f"{BASE}/jobs/{job.id}/result", json={"output": {"total": 3}},
            headers={"Authorization": f"Bearer {token}"})
        assert first.status_code == 200

        second = await anon_client.post(
            f"{BASE}/jobs/{job.id}/result", json={"output": {"total": 4}},
            headers={"Authorization": f"Bearer {token}"})
        assert second.status_code == 404

    @pytest.mark.asyncio
    async def test_no_token_and_a_wrong_token_look_the_same(self, session, anon_client):
        """A distinct "already used" would tell whoever holds a stolen token
        that it was genuine."""
        bot = await _bot(session)
        job = await _job(session, bot)
        await service.mint_result_token(session, job)
        await session.commit()

        responses = [
            await anon_client.post(f"{BASE}/jobs/{job.id}/result", json={"output": {}}),
            await anon_client.post(
                f"{BASE}/jobs/{job.id}/result", json={"output": {}},
                headers={"Authorization": "Bearer wrong"}),
            await anon_client.post(
                f"{BASE}/jobs/{uuid.uuid4()}/result", json={"output": {}},
                headers={"Authorization": "Bearer wrong"}),
        ]
        assert {r.status_code for r in responses} == {404}

    @pytest.mark.asyncio
    async def test_another_jobs_token_does_not_work(self, session, anon_client):
        bot = await _bot(session)
        mine = await _job(session, bot)
        theirs = await _job(session, bot)
        their_token = await service.mint_result_token(session, theirs)
        await service.mint_result_token(session, mine)
        await session.commit()

        resp = await anon_client.post(
            f"{BASE}/jobs/{mine.id}/result", json={"output": {}},
            headers={"Authorization": f"Bearer {their_token}"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_output_failing_the_schema_fails_the_job(self, session, anon_client):
        """And the token is burned anyway — a validation error must not leave it
        spendable for a second attempt with different content."""
        bot = await _bot(session, output_schema={
            "type": "object", "required": ["invoice_total"],
            "properties": {"invoice_total": {"type": "number"}},
        })
        job = await _job(session, bot)
        token = await service.mint_result_token(session, job)
        await session.commit()

        resp = await anon_client.post(
            f"{BASE}/jobs/{job.id}/result", json={"output": {"wrong": 1}},
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 422

        await session.refresh(job)
        assert job.status == "failed"
        assert job.result_token_used_at is not None

        retry = await anon_client.post(
            f"{BASE}/jobs/{job.id}/result", json={"output": {"invoice_total": 1}},
            headers={"Authorization": f"Bearer {token}"})
        assert retry.status_code == 404

    def test_token_comparison_never_matches_on_absence(self):
        assert tokens.matches(None, "hash") is False
        assert tokens.matches("token", None) is False
        assert tokens.matches("", "") is False

    @pytest.mark.asyncio
    async def test_progress_may_be_posted_repeatedly(self, session, anon_client):
        """Progress is many, results are one — and progress cannot complete the
        job, so replaying it achieves nothing."""
        bot = await _bot(session)
        job = await _job(session, bot)
        token = await service.mint_result_token(session, job)
        await session.commit()

        for percent in (10, 50, 90):
            resp = await anon_client.post(
                f"{BASE}/jobs/{job.id}/progress",
                json={"message": "working", "percent": percent},
                headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

        await session.refresh(job)
        assert job.status == "running"


# ── what a bot's output may do ────────────────────────────────────────────

class TestOutputAuthority:
    @pytest.mark.asyncio
    async def test_output_writes_case_variables_and_nothing_else(self, session):
        """Status, assignment and permissions are decisions the workflow and the
        operator own. A container that could make them would be a
        privilege-escalation path dressed as an integration."""
        from velaris_shared.db.models import CaseInstanceModel
        from tests.phase_hxsentinel._seed import seed_case

        bot = await _bot(session)
        case_id = await seed_case(session)
        case = await session.get(CaseInstanceModel, case_id)
        before_status = case.status
        await session.commit()

        job = await _job(session, bot, case_id=case_id, status="succeeded",
                         trigger="workflow")
        job.output = {"invoice_total": 42, "status": "closed",
                      "_intake_source": "hacked"}
        await session.commit()

        assert await service.apply_to_case(session, job) is True
        await session.commit()
        await session.refresh(case)

        assert case.data["invoice_total"] == 42
        assert case.status == before_status          # the column, untouched
        assert case.data.get("_intake_source") != "hacked"   # platform keys ours

    @pytest.mark.asyncio
    async def test_an_attended_job_is_proposed_not_applied(self, session):
        """The human is the commit point — that is the whole difference between
        an attended bot and an unattended one."""
        from velaris_shared.db.models import CaseInstanceModel
        from tests.phase_hxsentinel._seed import seed_case

        bot = await _bot(session)
        case_id = await seed_case(session)
        await session.commit()

        job = await _job(session, bot, case_id=case_id, status="succeeded",
                         trigger="agent")
        job.output = {"invoice_total": 99}
        await session.commit()

        assert await service.apply_to_case(session, job) is False
        case = await session.get(CaseInstanceModel, case_id)
        assert "invoice_total" not in (case.data or {})

    @pytest.mark.asyncio
    async def test_accepting_a_stale_proposal_is_a_409(self, session, client):
        """The same rule HxDraft already applies: the case changed underneath,
        so the operator reviews the new diff rather than silently overwriting."""
        from tests.phase_hxsentinel._seed import seed_case

        bot = await _bot(session)
        case_id = await seed_case(session)
        await session.commit()

        job = await _job(session, bot, case_id=case_id, status="succeeded",
                         trigger="agent")
        job.output = {"invoice_total": 99}
        await session.commit()

        resp = await client.post(f"{BASE}/jobs/{job.id}/accept",
                                 json={"expected_checksum": "stale"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_accepting_with_the_current_checksum_applies(self, session, client):
        from tests.phase_hxsentinel._seed import seed_case

        bot = await _bot(session)
        case_id = await seed_case(session)
        await session.commit()

        job = await _job(session, bot, case_id=case_id, status="succeeded",
                         trigger="agent")
        job.output = {"invoice_total": 99}
        await session.commit()

        proposal = (await client.get(f"{BASE}/jobs/{job.id}/proposal")).json()
        resp = await client.post(
            f"{BASE}/jobs/{job.id}/accept",
            json={"expected_checksum": proposal["checksum"]})
        assert resp.status_code == 200
        assert resp.json()["applied"] == ["invoice_total"]


# ── credentials ───────────────────────────────────────────────────────────

class TestCredentials:
    @pytest.mark.asyncio
    async def test_a_missing_credential_fails_the_job_rather_than_running_without(
        self, session,
    ):
        """A bot that runs without its password signs in as nobody and reports a
        confusing failure from inside a container, where nobody can see why."""
        bot = await _bot(session, credential_refs=["vault://tenant/acme/nope"])
        job = await _job(session, bot)
        with pytest.raises(credentials.CredentialError):
            await credentials.resolve_leases(
                session, job=job, refs=list(bot.credential_refs), ttl_seconds=60)

    @pytest.mark.asyncio
    async def test_a_lease_is_issued_and_revoked(self, session, client):
        from case_service.hxbridge.encryption import encrypt_credentials

        bot = await _bot(session, credential_refs=["vault://tenant/acme/portal"])
        session.add(BotCredentialModel(
            tenant_id=bot.tenant_id, ref="vault://tenant/acme/portal",
            value_enc=encrypt_credentials({"secret": "hunter2"}),
            created_by="admin",
        ))
        await session.commit()

        job = await _job(session, bot)
        env = await credentials.resolve_leases(
            session, job=job, refs=list(bot.credential_refs), ttl_seconds=60)
        await session.commit()
        assert env == {"PORTAL": "hunter2"}

        live = (await session.execute(
            select(BotCredentialLeaseModel).where(
                BotCredentialLeaseModel.job_id == job.id)
        )).scalars().all()
        assert len(live) == 1 and live[0].revoked_at is None

        assert await credentials.revoke_leases(session, job_id=job.id) == 1
        await session.commit()
        await session.refresh(live[0])
        assert live[0].revoked_at is not None

    @pytest.mark.asyncio
    async def test_a_credential_cannot_overwrite_a_reserved_variable(self, session):
        """A ref named `PATH` or `VELARIS_JOB_TOKEN` would be an injection into
        the container's environment."""
        bot = await _bot(session, credential_refs=["vault://x/PATH"])
        job = await _job(session, bot)
        with pytest.raises(credentials.CredentialError):
            await credentials.resolve_leases(
                session, job=job, refs=list(bot.credential_refs), ttl_seconds=60)

    @pytest.mark.asyncio
    async def test_the_stored_value_is_never_returned(self, session, client):
        resp = await client.put(f"{BASE}/credentials", json={
            "tenant_id": "default", "ref": "vault://tenant/acme/api",
            "secret": "super-secret-value"})
        assert resp.status_code == 200
        assert "super-secret-value" not in resp.text

        listed = await client.get(f"{BASE}/credentials")
        assert "super-secret-value" not in listed.text

    def test_redaction_removes_known_secrets(self):
        """A screenshot of a login screen contains the password, and so does an
        error message quoting the request."""
        out = credentials.redact("login failed for hunter2 at portal", ["hunter2"])
        assert "hunter2" not in out and "«redacted»" in out


# ── gating ────────────────────────────────────────────────────────────────

class TestGating:
    @pytest.mark.asyncio
    async def test_the_operator_surfaces_need_a_privilege(self, session, anon_client):
        bot = await _bot(session)
        for path, method in [
            (f"{BASE}", "get"),
            (f"{BASE}/jobs", "get"),
            (f"{BASE}/credentials", "get"),
        ]:
            assert (await getattr(anon_client, method)(path)).status_code in (401, 403)
        assert (await anon_client.post(
            f"{BASE}/{bot.id}/approve")).status_code in (401, 403)
        assert (await anon_client.post(
            f"{BASE}/{bot.id}/run", json={"input": {}})).status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_disabling_a_bot_cancels_its_live_jobs(self, session, client):
        """The kill switch: a bot that has started misbehaving stops now, not at
        the end of its queue."""
        bot = await _bot(session)
        job = await _job(session, bot, status="queued")
        await session.commit()

        resp = await client.post(f"{BASE}/{bot.id}/disable")
        assert resp.status_code == 200 and resp.json()["cancelled_jobs"] == 1

        await session.refresh(job)
        assert job.status == "cancelled"
