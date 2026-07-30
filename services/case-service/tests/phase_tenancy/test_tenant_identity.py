"""Canonical tenant identity — one answer for both platform conventions.

`case_instances.tenant_id` is a GUID FK; `case_sessions.tenant_id` and every
settings lookup use a slug; and `user_directory.tenant_id` is a bare String(64)
that may hold EITHER. These tests pin that a caller resolves correctly whichever
form their directory row carries, and that an unresolvable token denies rather
than matching everything.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import TenantModel
from case_service.tenancy import identity
from tests.conftest import TestSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cache():
    identity.clear_cache()
    yield
    identity.clear_cache()


async def _tenant(db, slug="acme") -> TenantModel:
    row = TenantModel(slug=slug, name=slug.title())
    db.add(row)
    await db.commit()
    return row


class TestResolve:
    async def test_resolves_a_slug(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.resolve_tenant(db, "acme") == (row.id, "acme")

    async def test_resolves_a_uuid_string(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.resolve_tenant(db, str(row.id)) == (row.id, "acme")

    async def test_unknown_token_resolves_to_nothing(self):
        async with TestSessionFactory() as db:
            assert await identity.resolve_tenant(db, "no-such-tenant") is None
            assert await identity.resolve_tenant(db, str(uuid.uuid4())) is None

    async def test_slug_helper_returns_canonical_slug_for_both_forms(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.tenant_slug(db, "acme") == "acme"
            assert await identity.tenant_slug(db, str(row.id)) == "acme"

    async def test_unknown_token_keeps_its_own_slug(self):
        """Falls back to the token, so an unregistered tenant matches nothing
        rather than silently becoming 'default'."""
        async with TestSessionFactory() as db:
            assert await identity.tenant_slug(db, "ghost") == "ghost"


class TestSameTenant:
    async def test_null_tenant_row_is_shared(self):
        """Platform-shared rows predate multi-tenancy; several features rely
        on them staying visible."""
        async with TestSessionFactory() as db:
            assert await identity.same_tenant(db, None, "anyone") is True

    async def test_uuid_token_deployment(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, str(row.id)) is True

    async def test_slug_token_deployment(self):
        """The case that used to fail closed: a slug on the user, a GUID on
        the case."""
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, "acme") is True

    async def test_other_tenant_is_denied_in_both_forms(self):
        async with TestSessionFactory() as db:
            mine = await _tenant(db, "acme")
            theirs = await _tenant(db, "globex")
            assert await identity.same_tenant(db, theirs.id, "acme") is False
            assert await identity.same_tenant(db, theirs.id, str(mine.id)) is False

    async def test_unresolvable_token_denies(self):
        """Fail closed: an unknown token must never read as 'any tenant'."""
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, "ghost") is False
            assert await identity.same_tenant(db, row.id, None) is False

    async def test_tenantless_caller_maps_to_the_default_tenant(self):
        """A deliberate widening. The broken GUID compare denied a tenant-less
        caller everything; the slug world (case_sessions, messages) has always
        treated a missing token as the `default` tenant. This makes the two
        agree rather than leaving one of them wrong."""
        async with TestSessionFactory() as db:
            default = await _tenant(db, "default")
            other = await _tenant(db, "acme")
            assert await identity.same_tenant(db, default.id, None) is True
            assert await identity.same_tenant(db, other.id, None) is False

    async def test_cache_is_dropped_on_demand(self):
        async with TestSessionFactory() as db:
            assert await identity.resolve_tenant(db, "acme") is None   # cached miss
            row = await _tenant(db)
            identity.clear_cache()
            assert await identity.resolve_tenant(db, "acme") == (row.id, "acme")


class TestEnforcementDefault:
    async def test_case_gates_enforce_unless_told_otherwise(self):
        """Silence must not mean 'open' — in shadow every case gate is
        advisory, including meet.recording.view."""
        from case_service.config import Settings
        assert Settings.model_fields["hxguard_case_enforcement"].default == "enforce"
