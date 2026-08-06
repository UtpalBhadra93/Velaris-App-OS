"""Retention enforcement.

The tests that matter are the refusals. A retention module's dangerous failure
is not deleting too little — it is reporting success while doing nothing, or
deleting something it should never have touched.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.compliance import retention
from case_service.db.models import (
    CaseEventLogModel,
    CaseInstanceModel,
    RetentionPolicyModel,
)

pytestmark = pytest.mark.asyncio


def _old(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _policy(session, *, resource_type="event_log", days=365,
                  action=retention.DELETE, enabled=True):
    row = RetentionPolicyModel(
        name=f"test-{resource_type}", resource_type=resource_type,
        retention_days=days, action=action, enabled=enabled,
    )
    session.add(row)
    await session.flush()
    return row


async def _case(session, *, status="closed", age_days=1000):
    row = CaseInstanceModel(
        case_type_id=uuid.uuid4(), case_type_version=1, status=status,
        # NULL, not a slug: case_instances.tenant_id is a GUID column, and
        # these tests exercise platform-wide policies. Tenant scoping has its
        # own class, which creates real tenant rows.
        priority="medium", data={}, created_by="test", tenant_id=None,
        updated_at=_old(age_days),
    )
    session.add(row)
    await session.flush()
    return row


class TestRefusals:
    async def test_unknown_action_is_refused(self, session):
        policy = await _policy(session, action="shred")
        with pytest.raises(retention.RetentionError, match="unknown retention action"):
            await retention.apply_policy(session, policy)

    async def test_unknown_resource_type_is_refused(self, session):
        policy = await _policy(session, resource_type="invoices")
        with pytest.raises(retention.RetentionError, match="no handler"):
            await retention.apply_policy(session, policy)

    async def test_disabled_policy_does_nothing_and_says_so(self, session):
        policy = await _policy(session, enabled=False)
        out = await retention.apply_policy(session, policy, apply=True)
        assert out["applied"] is False
        assert "disabled" in out["skipped"]

    async def test_the_audit_chain_is_never_a_target(self):
        """Migration 082 makes the chain immutable at the DB level."""
        assert "case_audit_log_chain" in retention.FORBIDDEN_TABLES
        assert "case_audit_log_chain" not in retention.HANDLERS


class TestDryRun:
    async def test_dry_run_counts_without_deleting(self, session):
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(), activity_type="task", activity="x", timestamp=_old(500),
        ))
        await session.flush()

        policy = await _policy(session, resource_type="event_log", days=365)
        out = await retention.apply_policy(session, policy, apply=False)

        assert out["rows"] == 1
        assert out["applied"] is False
        remaining = (await session.execute(select(CaseEventLogModel))).scalars().all()
        assert len(remaining) == 1, "dry run must not delete"

    async def test_dry_run_does_not_stamp_last_run(self, session):
        policy = await _policy(session)
        await retention.apply_policy(session, policy, apply=False)
        assert policy.last_run_at is None

    async def test_run_all_defaults_to_dry(self, session):
        await _policy(session)
        out = await retention.run_all(session)
        assert out["dry_run"] is True


class TestApply:
    async def test_deletes_rows_past_the_cutoff(self, session):
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(), activity_type="task", activity="old", timestamp=_old(500),
        ))
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(), activity_type="task", activity="recent", timestamp=_old(10),
        ))
        await session.flush()

        policy = await _policy(session, resource_type="event_log", days=365)
        out = await retention.apply_policy(session, policy, apply=True)
        await session.flush()

        assert out["rows"] == 1
        kept = (await session.execute(select(CaseEventLogModel))).scalars().all()
        assert [k.activity for k in kept] == ["recent"]

    async def test_stamps_last_run_at(self, session):
        policy = await _policy(session, resource_type="event_log")
        await retention.apply_policy(session, policy, apply=True)
        assert policy.last_run_at is not None


class TestOpenCasesAreSafe:
    async def test_an_open_case_is_never_deleted_however_old(self, session):
        """No retention period justifies destroying live work."""
        await _case(session, status="in_progress", age_days=5000)
        policy = await _policy(session, resource_type="resolved_cases", days=30)

        out = await retention.apply_policy(session, policy, apply=True)
        await session.flush()

        assert out["rows"] == 0
        assert (await session.execute(select(CaseInstanceModel))).scalars().all()

    async def test_a_closed_case_past_the_cutoff_is_deleted(self, session):
        await _case(session, status="closed", age_days=5000)
        policy = await _policy(session, resource_type="resolved_cases", days=30)

        out = await retention.apply_policy(session, policy, apply=True)
        assert out["rows"] == 1


class TestRunAll:
    async def test_one_failing_policy_does_not_stop_the_others(self, session):
        """A policy with an unknown resource type must not block the valid ones."""
        await _policy(session, resource_type="invoices", action=retention.DELETE)
        await _policy(session, resource_type="event_log", action=retention.DELETE)

        out = await retention.run_all(session, apply=False)

        assert len(out["errors"]) == 1
        assert any(p.get("error") for p in out["policies"])
        assert any(p.get("error") is None for p in out["policies"])


class TestArchiveUsesThePolicysOwnPeriod:
    """`archive` = delete on the policy's own clock. No separate table."""

    async def test_archive_honours_the_policy_days(self, session):
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(), activity_type="task", activity="old", timestamp=_old(1200),
        ))
        await session.flush()

        policy = await _policy(session, resource_type="event_log",
                               action=retention.ARCHIVE, days=1095)
        out = await retention.apply_policy(session, policy, apply=True)

        assert out["effective_days"] == 1095
        assert out["rows"] == 1

    async def test_the_screen_number_is_the_run_number(self, session):
        """No divergence: what the admin panel shows is what runs."""
        policy = await _policy(session, resource_type="event_log",
                               action=retention.ARCHIVE, days=1095)
        out = await retention.apply_policy(session, policy, apply=False)

        assert out["effective_days"] == policy.retention_days
        assert "policy_days" not in out
        assert "archive_period_applied" not in out

    async def test_archive_keeps_data_inside_its_period(self, session):
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(), activity_type="task", activity="recent", timestamp=_old(100),
        ))
        await session.flush()

        policy = await _policy(session, resource_type="event_log",
                               action=retention.ARCHIVE, days=1095)
        out = await retention.apply_policy(session, policy, apply=True)
        await session.flush()

        assert out["rows"] == 0
        assert len((await session.execute(select(CaseEventLogModel))).scalars().all()) == 1

    async def test_delete_and_archive_arithmetic_is_identical(self, session):
        """They differ in intent, not in maths — the seed default is the only difference."""
        a = await _policy(session, resource_type="event_log",
                          action=retention.ARCHIVE, days=500)
        d = await _policy(session, resource_type="audit_log",
                          action=retention.DELETE, days=500)
        oa = await retention.apply_policy(session, a, apply=False)
        od = await retention.apply_policy(session, d, apply=False)
        assert oa["effective_days"] == od["effective_days"] == 500

    async def test_seed_default_is_seven_years(self):
        assert retention.DEFAULT_ARCHIVE_DAYS == 2555


class TestAnonymize:
    """`anonymize` keeps the row and removes the identity.

    Not a softer delete — a different operation. Deleting a resolved case
    destroys a labelled training example; anonymising keeps it teaching.
    """

    async def _old_case(self, session, data=None):
        row = CaseInstanceModel(
            case_type_id=uuid.uuid4(), case_type_version=1, status="closed",
            priority="medium", tenant_id=None, created_by="alice@example.com",
            updated_at=_old(5000),
            data=data if data is not None else {
                "submitter_email": "alice@example.com",
                "_raw_payload": {"phone": "07700900000"},
                "claim_amount": 1200,
            },
        )
        session.add(row)
        await session.flush()
        return row

    async def test_the_row_survives(self, session):
        case = await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)

        out = await retention.apply_policy(session, policy, apply=True)
        await session.flush()

        assert out["rows"] == 1
        assert await session.get(CaseInstanceModel, case.id) is not None

    async def test_identity_is_removed(self, session):
        case = await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)
        await retention.apply_policy(session, policy, apply=True)
        await session.flush()
        await session.refresh(case)

        assert case.data["submitter_email"] == "[anonymized]"
        assert case.data["_raw_payload"] == "[anonymized]"
        assert case.created_by == "anonymized"

    async def test_non_pii_is_kept_so_the_model_still_learns(self, session):
        """HxSentinel features must survive — that is the whole point."""
        case = await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)
        await retention.apply_policy(session, policy, apply=True)
        await session.flush()
        await session.refresh(case)

        assert case.data["claim_amount"] == 1200

    async def test_keys_are_replaced_not_removed(self, session):
        """A missing key and a scrubbed key must not look identical."""
        case = await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)
        await retention.apply_policy(session, policy, apply=True)
        await session.flush()
        await session.refresh(case)

        assert "submitter_email" in case.data

    async def test_it_is_idempotent(self, session):
        await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)

        first = await retention.apply_policy(session, policy, apply=True)
        await session.flush()
        second = await retention.apply_policy(session, policy, apply=True)

        assert first["rows"] == 1
        assert second["rows"] == 0, "an anonymised case must not be re-scrubbed"

    async def test_open_cases_are_untouched(self, session):
        case = await self._old_case(session)
        case.status = "in_progress"
        await session.flush()

        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)
        out = await retention.apply_policy(session, policy, apply=True)
        assert out["rows"] == 0

    async def test_dry_run_changes_nothing(self, session):
        case = await self._old_case(session)
        policy = await _policy(session, resource_type="resolved_cases",
                               action=retention.ANONYMIZE, days=30)

        out = await retention.apply_policy(session, policy, apply=False)
        await session.refresh(case)

        assert out["rows"] == 1
        assert case.data["submitter_email"] == "alice@example.com"

    async def test_refused_on_resource_types_with_no_identity(self, session):
        policy = await _policy(session, resource_type="event_log",
                               action=retention.ANONYMIZE, days=30)
        with pytest.raises(retention.RetentionError, match="resolved_cases only"):
            await retention.apply_policy(session, policy)


async def _tenant(session, slug):
    """A real tenant row — retention resolves a policy's tenant token through
    `tenancy.identity`, because policies store a slug and cases store a GUID."""
    from case_service.db.models import TenantModel
    from case_service.tenancy import identity

    identity.clear_cache()
    row = TenantModel(name=slug, slug=slug)
    session.add(row)
    await session.flush()
    return row


class TestTenantScoping:
    """One tenant's retention period must not become another's.

    `resource_type` carried a bare UNIQUE until migration 153, so the platform
    could hold exactly one policy per resource type — and retention periods are
    a per-customer contractual commitment.
    """

    async def _case_for(self, session, tenant_id, *, age_days=5000):
        row = CaseInstanceModel(
            case_type_id=uuid.uuid4(), case_type_version=1, status="closed",
            priority="medium", data={}, created_by="test",
            tenant_id=tenant_id, updated_at=_old(age_days),
        )
        session.add(row)
        await session.flush()
        return row

    async def test_a_policy_only_touches_its_own_tenant(self, session):
        a = await _tenant(session, "tenant-a")
        b = await _tenant(session, "tenant-b")
        keep = await self._case_for(session, b.id)
        await self._case_for(session, a.id)

        # The policy stores the SLUG; the case stores the GUID. Resolving
        # between them is the whole reason tenancy.identity exists.
        policy = await _policy(session, resource_type="resolved_cases", days=30)
        policy.tenant_id = "tenant-a"
        await session.flush()

        out = await retention.apply_policy(session, policy, apply=True)
        await session.flush()

        assert out["rows"] == 1
        survivors = (await session.execute(select(CaseInstanceModel))).scalars().all()
        assert [s.id for s in survivors] == [keep.id]

    async def test_a_platform_default_policy_spans_tenants(self, session):
        """tenant_id NULL = the default, applied where a tenant has none."""
        a = await _tenant(session, "tenant-a")
        b = await _tenant(session, "tenant-b")
        await self._case_for(session, a.id)
        await self._case_for(session, b.id)

        policy = await _policy(session, resource_type="resolved_cases", days=30)
        assert policy.tenant_id is None

        out = await retention.apply_policy(session, policy, apply=True)
        assert out["rows"] == 2

    async def test_run_all_filters_policies_by_tenant(self, session):
        await _tenant(session, "tenant-a")
        a = await _policy(session, resource_type="event_log")
        a.tenant_id = "tenant-a"
        b = await _policy(session, resource_type="event_log")
        b.tenant_id = "tenant-b"
        await session.flush()

        out = await retention.run_all(session, tenant_id="tenant-a")
        assert [p["resource_type"] for p in out["policies"]] == ["event_log"]
        assert out["tenant_id"] == "tenant-a"

    async def test_the_result_names_the_tenant(self, session):
        await _tenant(session, "tenant-a")
        policy = await _policy(session, resource_type="event_log")
        policy.tenant_id = "tenant-a"
        await session.flush()
        out = await retention.apply_policy(session, policy, apply=False)
        assert out["tenant_id"] == "tenant-a"


class TestDefaultDefersToOverrides:
    """A platform default must not overrule a tenant's own retention period.

    Migration 153 made policies per-tenant and the admin screen edits them, but
    until 2026-08-06 execution never learned the rule: `run_all` ran the default
    completely unscoped, so a 365-day default deleted the rows of a tenant whose
    own policy said 2555 days. The override was stored, displayed, and ignored
    at the only moment it mattered — the worst shape for a compliance control,
    because the screen said it was enforced.

    Retention periods are a contractual and jurisdictional commitment. Deleting
    early is not a tidy-up; it is a breach, and it is irreversible.
    """

    async def _case_for(self, session, tenant_id, *, age_days=5000):
        row = CaseInstanceModel(
            case_type_id=uuid.uuid4(), case_type_version=1, status="closed",
            priority="medium", data={}, created_by="test",
            tenant_id=tenant_id, updated_at=_old(age_days),
        )
        session.add(row)
        await session.flush()
        return row

    async def test_default_skips_a_tenant_with_its_own_policy(self, session):
        a = await _tenant(session, "tenant-a")          # has a long policy
        b = await _tenant(session, "tenant-b")          # relies on the default
        protected = await self._case_for(session, a.id)
        await self._case_for(session, b.id)

        own = await _policy(session, resource_type="resolved_cases", days=99_999)
        own.tenant_id = "tenant-a"
        await _policy(session, resource_type="resolved_cases", days=30)  # default
        await session.flush()

        out = await retention.run_all(session, apply=True)
        await session.flush()

        survivors = (await session.execute(select(CaseInstanceModel))).scalars().all()
        assert [s.id for s in survivors] == [protected.id], (
            "the default deleted a case belonging to a tenant whose own policy "
            "keeps it — the override was overruled by the thing it overrides"
        )
        default = next(p for p in out["policies"] if p["tenant_id"] is None)
        assert default["excluded_tenants"] == 1

    async def test_tenant_less_rows_stay_with_the_default(self, session):
        """`NOT IN` drops NULLs, and NULL-tenant rows are the default's own.

        SQL never returns true for `NULL <> x`, so a bare exclusion list would
        silently stop covering rows that belong to no tenant — and they would be
        retained forever by a policy reporting success.
        """
        a = await _tenant(session, "tenant-a")
        await self._case_for(session, a.id)
        await self._case_for(session, None)             # tenant-less

        own = await _policy(session, resource_type="resolved_cases", days=99_999)
        own.tenant_id = "tenant-a"
        await _policy(session, resource_type="resolved_cases", days=30)
        await session.flush()

        out = await retention.run_all(session, apply=True)

        default = next(p for p in out["policies"] if p["tenant_id"] is None)
        assert default["rows"] == 1, "the tenant-less row must still be covered"

    async def test_an_override_only_shields_its_own_resource_type(self, session):
        """Overriding case retention says nothing about the event log."""
        a = await _tenant(session, "tenant-a")
        session.add(CaseEventLogModel(
            case_id=uuid.uuid4(), case_type_id=uuid.uuid4(),
            activity_type="task", activity="x", timestamp=_old(5000),
        ))
        await self._case_for(session, a.id)

        own = await _policy(session, resource_type="resolved_cases", days=99_999)
        own.tenant_id = "tenant-a"
        await _policy(session, resource_type="resolved_cases", days=30)
        await _policy(session, resource_type="event_log", days=30)
        await session.flush()

        out = await retention.run_all(session, apply=True)

        ev = next(p for p in out["policies"] if p["resource_type"] == "event_log")
        assert ev["rows"] == 1
        assert ev.get("excluded_tenants") == 0

    async def test_a_disabled_override_does_not_shield(self, session):
        """A switched-off policy is not a retention commitment."""
        a = await _tenant(session, "tenant-a")
        await self._case_for(session, a.id)

        own = await _policy(session, resource_type="resolved_cases",
                            days=99_999, enabled=False)
        own.tenant_id = "tenant-a"
        await _policy(session, resource_type="resolved_cases", days=30)
        await session.flush()

        out = await retention.run_all(session, apply=True)

        default = next(p for p in out["policies"] if p["tenant_id"] is None)
        assert default["excluded_tenants"] == 0
        assert default["rows"] == 1

    async def test_the_default_is_unchanged_when_nobody_overrides(self, session):
        """No overrides must mean no behaviour change at all."""
        a = await _tenant(session, "tenant-a")
        b = await _tenant(session, "tenant-b")
        await self._case_for(session, a.id)
        await self._case_for(session, b.id)
        await _policy(session, resource_type="resolved_cases", days=30)
        await session.flush()

        out = await retention.run_all(session, apply=True)

        default = next(p for p in out["policies"] if p["tenant_id"] is None)
        assert default["rows"] == 2
        assert default["excluded_tenants"] == 0

    async def test_dry_run_reports_the_narrowing(self, session):
        """An operator must see the default was narrowed before it deletes."""
        a = await _tenant(session, "tenant-a")
        await self._case_for(session, a.id)

        own = await _policy(session, resource_type="resolved_cases", days=99_999)
        own.tenant_id = "tenant-a"
        await _policy(session, resource_type="resolved_cases", days=30)
        await session.flush()

        out = await retention.run_all(session, apply=False)

        default = next(p for p in out["policies"] if p["tenant_id"] is None)
        assert out["dry_run"] is True
        assert default["rows"] == 0
        assert default["excluded_tenants"] == 1


class TestSchedulerDefaults:
    """Two switches, both off. A convenience that deletes data is not one."""

    async def test_both_flags_default_off(self):
        from case_service.config import get_settings

        s = get_settings()
        assert s.retention_scheduler_enabled is False
        assert s.retention_scheduler_apply is False

    async def test_enabling_the_scheduler_does_not_imply_applying(self):
        from case_service.integrations.retention_runner import RetentionRunner

        runner = RetentionRunner(None, apply=False)
        assert runner._apply is False

    async def test_interval_is_floored(self):
        from case_service.integrations.retention_runner import RetentionRunner

        assert RetentionRunner(None, interval_seconds=1)._interval >= 3600


class TestUnscopableResources:
    """`case_audit_log` and `security_events` have no tenant column at all.

    Silently applying a tenant-scoped policy to them would delete EVERY
    tenant's rows while the policy claims to be scoped to one.
    """

    async def test_a_tenant_scoped_audit_policy_is_refused(self, session):
        await _tenant(session, "tenant-a")
        policy = await _policy(session, resource_type="audit_log")
        policy.tenant_id = "tenant-a"
        await session.flush()

        with pytest.raises(retention.RetentionError, match="no tenant column"):
            await retention.apply_policy(session, policy, apply=True)

    async def test_a_platform_wide_audit_policy_still_runs(self, session):
        policy = await _policy(session, resource_type="audit_log")
        assert policy.tenant_id is None
        out = await retention.apply_policy(session, policy, apply=False)
        assert "error" not in out

    async def test_an_unresolvable_tenant_is_refused_not_widened(self, session):
        """A filter that fails to resolve must deny, never match everything."""
        from case_service.tenancy import identity

        identity.clear_cache()
        policy = await _policy(session, resource_type="event_log")
        policy.tenant_id = "no-such-tenant"
        await session.flush()

        with pytest.raises(retention.RetentionError, match="resolves to no tenant"):
            await retention.apply_policy(session, policy, apply=True)
