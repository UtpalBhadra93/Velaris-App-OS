"""One tenant per mailbox — added 2026-09-17.

Every email route was tenant-blind: any `admin` (a TENANT admin on a shared
install) listed, edited and deleted every tenant's mailboxes and their
credentials, read every tenant's mail, sent through another tenant's address,
and polled all of them. These pin the rule in `case_service/mail/tenancy.py`:
a mailbox, its mail and its templates belong to one tenant; an unassigned row
belongs to the default tenant, not to everyone; a superadmin sees all.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    EmailAccountModel, EmailMessageModel, EmailTemplateModel, TenantModel,
    UserDirectoryModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import ADMIN_HEADERS, TestSessionFactory

pytestmark = pytest.mark.asyncio


def _h(username: str, roles=("admin",)) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(user_id=str(uuid.uuid4()), username=username, roles=list(roles),
                           secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {tok}"}


def _account(name, tenant, default=False):
    return EmailAccountModel(
        id=uuid.uuid4(), name=name, address=f"{name}@mail.test", smtp_host="smtp.test",
        smtp_port=587, smtp_username=name, smtp_password="secret-" + name,
        is_active=True, is_default_outbound=default, tenant_id=tenant)


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def mail():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        db.add_all([
            TenantModel(id=uuid.uuid4(), slug="default", name="Default"),
            TenantModel(id=uuid.uuid4(), slug="alpha", name="Alpha"),
            TenantModel(id=uuid.uuid4(), slug="beta", name="Beta"),
            UserDirectoryModel(id=uuid.uuid4(), user_id="alice", tenant_id="alpha"),
            UserDirectoryModel(id=uuid.uuid4(), user_id="bruno", tenant_id="beta"),
        ])
        a = _account("alphabox", "alpha", default=True)
        b = _account("betabox", "beta", default=True)
        legacy = _account("legacybox", None, default=True)
        db.add_all([a, b, legacy])
        await db.flush()
        am = EmailMessageModel(id=uuid.uuid4(), direction="inbound", account_id=a.id,
                               message_id="<a1@x>", subject="alpha secret", tenant_id="alpha")
        bm = EmailMessageModel(id=uuid.uuid4(), direction="inbound", account_id=b.id,
                               message_id="<b1@x>", subject="beta secret", tenant_id="beta")
        db.add_all([am, bm,
                    EmailTemplateModel(id=uuid.uuid4(), name="alpha tpl", subject="s",
                                       body_text="b", tenant_id="alpha"),
                    EmailTemplateModel(id=uuid.uuid4(), name="beta tpl", subject="s",
                                       body_text="b", tenant_id="beta")])
        await db.commit()
        yield {"a": a.id, "b": b.id, "legacy": legacy.id, "am": am.id, "bm": bm.id}
    tenancy.clear_cache()


async def test_each_tenant_lists_only_its_own_mailboxes(api, mail):
    names = {x["name"] for x in (await api.get("/api/v1/email/accounts", headers=_h("alice"))).json()}
    assert names == {"alphabox"}, "not beta's, and not the unassigned (default tenant's) one"
    names = {x["name"] for x in (await api.get("/api/v1/email/accounts", headers=ADMIN_HEADERS)).json()}
    assert {"alphabox", "betabox", "legacybox"} <= names


async def test_an_unassigned_mailbox_belongs_to_the_default_tenant_only(api, mail):
    no_claim = _h("local-admin-without-directory-row")
    names = {x["name"] for x in (await api.get("/api/v1/email/accounts", headers=no_claim)).json()}
    assert names == {"legacybox"}


@pytest.mark.parametrize("method,path,body", [
    ("patch", "/api/v1/email/accounts/{b}", {"name": "x", "address": "x@x", "smtp_host": "h"}),
    ("patch", "/api/v1/email/accounts/{b}/deactivate", None),
    ("delete", "/api/v1/email/accounts/{b}", None),
    ("post", "/api/v1/email/accounts/{b}/test-connection", None),
    ("get", "/api/v1/email/messages/{bm}", None),
    ("post", "/api/v1/email/messages/{bm}/mark-read", None),
])
async def test_another_tenants_mailbox_and_mail_are_absent(api, mail, method, path, body):
    url = path.format(**mail)
    kw = {"json": body} if body is not None else {}
    r = await getattr(api, method)(url, headers=_h("alice"), **kw)
    assert r.status_code == 404, (url, r.status_code, r.text)
    async with TestSessionFactory() as db:
        acc = await db.get(EmailAccountModel, mail["b"])
        assert acc is not None and acc.is_active and acc.name == "betabox"


async def test_a_mailbox_is_created_in_the_callers_tenant_whatever_the_body_says(api, mail):
    r = await api.post("/api/v1/email/accounts", headers=_h("alice"), json={
        "name": "sneaky", "address": "s@x", "smtp_host": "h", "tenant_id": "beta"})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "alpha"


async def test_a_tenant_admin_cannot_move_a_mailbox_to_another_tenant(api, mail):
    r = await api.patch(f"/api/v1/email/accounts/{mail['a']}", headers=_h("alice"), json={
        "name": "alphabox", "address": "alphabox@mail.test", "smtp_host": "smtp.test",
        "tenant_id": "beta"})
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "alpha"


async def test_the_default_outbound_mailbox_is_per_tenant(api, mail):
    r = await api.post("/api/v1/email/accounts", headers=_h("alice"), json={
        "name": "alpha2", "address": "a2@x", "smtp_host": "h", "is_default_outbound": True})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        assert (await db.get(EmailAccountModel, mail["a"])).is_default_outbound is False
        assert (await db.get(EmailAccountModel, mail["b"])).is_default_outbound is True, \
            "alpha choosing a default must not clear beta's"
        assert (await db.get(EmailAccountModel, mail["legacy"])).is_default_outbound is True


async def test_send_refuses_another_tenants_mailbox_and_defaults_to_its_own(api, mail, monkeypatch):
    r = await api.post("/api/v1/email/send", headers=_h("alice"), json={
        "account_id": str(mail["b"]), "to_addresses": ["x@y.test"], "subject": "s",
        "body_text": "b", "dry_run": True})
    assert r.status_code == 404, r.text

    from case_service.api.routers import email as email_router

    used = {}

    async def fake_send(session, **kw):
        used["account"] = kw["account"].name
        return EmailMessageModel(id=uuid.uuid4(), direction="outbound", status="dry_run",
                                 message_id="<x>", subject="s")

    monkeypatch.setattr(email_router.service, "send", fake_send)
    r = await api.post("/api/v1/email/send", headers=_h("alice"), json={
        "to_addresses": ["x@y.test"], "subject": "s", "body_text": "b", "dry_run": True})
    assert r.status_code == 201, r.text
    assert used["account"] == "alphabox"


async def test_messages_stats_and_templates_are_scoped(api, mail):
    subjects = {m["subject"] for m in (await api.get("/api/v1/email/messages",
                                                     headers=_h("alice"))).json()}
    assert subjects == {"alpha secret"}
    stats = (await api.get("/api/v1/email/inbox/stats", headers=_h("alice"))).json()
    assert stats["unread_inbound"] == 1
    tpls = {t["name"] for t in (await api.get("/api/v1/email/templates?active_only=false",
                                              headers=_h("alice"))).json()}
    assert tpls == {"alpha tpl"}


async def test_poll_now_polls_only_the_callers_mailboxes(api, mail, monkeypatch):
    polled = []

    async def fake_poll(account, factory, service):
        polled.append(account.name)
        return 0

    monkeypatch.setattr("case_service.mail.worker.poll_one_account", fake_poll)
    r = await api.post("/api/v1/email/poll-now", headers=_h("alice"))
    assert r.status_code == 200, r.text
    assert polled == ["alphabox"]


async def test_simulate_inbound_needs_the_privilege_and_lands_in_the_callers_tenant(api, mail):
    r = await api.post("/api/v1/email/simulate-inbound", headers=_h("carol", roles=("case_worker",)),
                       json={"subject": "planted"})
    assert r.status_code == 403
    r = await api.post("/api/v1/email/simulate-inbound", headers=_h("alice"),
                       json={"subject": "test"})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        row = (await db.execute(select(EmailMessageModel).where(
            EmailMessageModel.subject == "test"))).scalar_one()
        assert row.tenant_id == "alpha"
