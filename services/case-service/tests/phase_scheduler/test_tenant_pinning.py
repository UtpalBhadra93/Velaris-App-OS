"""A schedule's tenant is not the caller's to choose.

Found by probing the live service on 2026-08-20: `POST /scheduler` wrote
`tenant_id` straight from the request body. That field is not decoration — every
action resolves the bots, connectors, cases and sync destinations it may touch
*against the schedule's own tenant*, so one string in a request body was a
cross-tenant reach with an approval gate that would be satisfied by the wrong
tenant's admin.

The bots router had the same shape, and its act paths had no tenant check at
all: a definition registered against somebody else's tenant could be approved,
disabled or run by them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from velaris_shared.db.models import TenantModel
from case_service.tenancy.identity import (
    caller_tenant_or_refuse,
    clear_cache,
)


@dataclass
class _User:
    tenant_id: str | None
    username: str = "someone"


async def _tenant(session, slug: str) -> TenantModel:
    row = TenantModel(slug=slug, name=slug)
    session.add(row)
    await session.commit()
    clear_cache()
    return row


class TestCallerTenant:
    @pytest.mark.asyncio
    async def test_a_caller_may_write_into_their_own_tenant(self, session):
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        got = await caller_tenant_or_refuse(session, _User(mine.slug), mine.slug)
        assert got == mine.slug

    @pytest.mark.asyncio
    async def test_either_form_of_the_token_resolves(self, session):
        """A tenant token is legitimately a slug OR a UUID depending on how the
        directory was populated — comparing raw strings fails closed."""
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        assert await caller_tenant_or_refuse(
            session, _User(str(mine.id)), mine.slug) == mine.slug

    @pytest.mark.asyncio
    async def test_another_tenant_is_refused(self, session):
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        theirs = await _tenant(session, f"theirs-{uuid.uuid4().hex[:6]}")
        with pytest.raises(PermissionError):
            await caller_tenant_or_refuse(session, _User(mine.slug), theirs.slug)

    @pytest.mark.asyncio
    async def test_an_unregistered_tenant_string_is_refused(self, session):
        """"Not a tenant at all" must not read as "no opinion, allow"."""
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        with pytest.raises(PermissionError):
            await caller_tenant_or_refuse(session, _User(mine.slug), "made-up")

    @pytest.mark.asyncio
    async def test_default_is_attributed_to_the_callers_own_tenant(self, session):
        """A UI with no opinion sends "default"; that must not park a tenanted
        user's schedule in the platform-wide bucket."""
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        assert await caller_tenant_or_refuse(
            session, _User(mine.slug), "default") == mine.slug

    @pytest.mark.asyncio
    async def test_a_token_with_no_tenant_gets_the_default_tenant(self, session):
        """Not "anything it asks for". An absent claim is the platform-default
        tenant; reading "unset" as "any tenant" is how a missing claim turns into
        an authorization bypass."""
        assert await caller_tenant_or_refuse(
            session, _User(None), "default") == "default"
        with pytest.raises(PermissionError):
            await caller_tenant_or_refuse(session, _User(None), "somebody-else")

    @pytest.mark.asyncio
    async def test_superadmin_may_write_anywhere(self, session):
        """Crossing is a role somebody was deliberately given — the installer and
        platform operators hold it, and the write is auditable."""
        @dataclass
        class _Super:
            tenant_id: str | None = None
            username: str = "root"
            roles: tuple = ("admin", "superadmin")

        theirs = await _tenant(session, f"theirs-{uuid.uuid4().hex[:6]}")
        assert await caller_tenant_or_refuse(
            session, _Super(), theirs.slug) == theirs.slug


class TestBotActPaths:
    @pytest.mark.asyncio
    async def test_another_tenants_bot_is_not_approvable(self, client, session):
        """404, not 403: "exists but is not yours" is an answer nobody outside
        that tenant needs."""
        from case_service.api.routers.bots import _mine_or_404
        from velaris_shared.db.models import BotDefinitionModel
        from fastapi import HTTPException

        theirs = await _tenant(session, f"theirs-{uuid.uuid4().hex[:6]}")
        mine = await _tenant(session, f"mine-{uuid.uuid4().hex[:6]}")
        bot = BotDefinitionModel(
            tenant_id=theirs.slug, name=f"bot-{uuid.uuid4().hex[:6]}",
            description="", image="registry.test/x", digest="sha256:" + "a" * 64,
            declared_command=[], declared_env={}, credential_refs={},
            input_schema={}, output_schema={}, granted_domains=[],
            status="draft", created_by="them")
        session.add(bot)
        await session.commit()

        with pytest.raises(HTTPException) as raised:
            await _mine_or_404(session, _User(mine.slug), bot)
        assert raised.value.status_code == 404
