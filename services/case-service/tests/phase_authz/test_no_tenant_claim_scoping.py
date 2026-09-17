"""A caller with no tenant claim is the default tenant — never every tenant.

Found by the 2026-09-17 audit. `is_superadmin` states the rule ("a token with no
tenant is not a superuser; `same_tenant` reads it as the platform-default
tenant") and the single-case read follows it, but nine list routes skipped
their tenant clause whenever `user.tenant_id` was empty. On a multi-tenant
install, any holder of the right privilege with no user-directory row — a local
account, a service token — listed every tenant's rows. Two of them also let such
a caller choose any tenant with `?tenant_id=`.

Each test here puts a row in another tenant and asks as a plain `admin` (not a
superadmin) whose token carries no tenant. The row must not appear. A
superadmin still sees it — that is the documented break-glass.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from velaris_shared.db.models import Base, CaseTypeModel, TenantModel

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import ADMIN_HEADERS, LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio

T = Base.metadata.tables


def _no_claim_admin(roles=("admin",)) -> dict[str, str]:
    """`admin`, not `superadmin`, and a username with no directory row."""
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(user_id=str(uuid.uuid4()), username=f"local-{uuid.uuid4().hex[:6]}",
                           roles=list(roles), secret=s.auth_secret,
                           private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {tok}"}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def other():
    """Tenant `elsewhere`, with one row of each kind the routes list."""
    tenancy.clear_cache()
    now = datetime.now(timezone.utc)
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="elsewhere", name="Elsewhere")
        db.add(t)
        await db.flush()
        ids = {k: uuid.uuid4() for k in ("ep", "conv", "ct", "tree", "conn", "pii", "bk")}
        await db.execute(insert(T["user_directory"]).values(
            id=uuid.uuid4(), user_id="elsewhere-user", tenant_id="elsewhere"))
        await db.execute(insert(T["escalation_trees"]).values(
            id=ids["tree"], name="Elsewhere tree", tenant_id="elsewhere"))
        await db.execute(insert(T["connector_registry"]).values(
            id=ids["conn"], name="Elsewhere connector", connector_type="rest",
            tenant_id="elsewhere"))
        await db.execute(insert(T["pii_findings"]).values(
            id=ids["pii"], resource_type="form", resource_id=uuid.uuid4(),
            field_path="elsewhere.ssn", category="national_id", tenant_id="elsewhere"))
        await db.execute(insert(T["backup_records"]).values(
            id=ids["bk"], location="s3://elsewhere/backup", tenant_id="elsewhere"))
        await db.execute(insert(T["channel_endpoints"]).values(
            id=ids["ep"], tenant_id="elsewhere", channel="sms", provider="twilio",
            address="+15550000001"))
        await db.execute(insert(T["channel_conversations"]).values(
            id=ids["conv"], tenant_id="elsewhere", endpoint_id=ids["ep"],
            peer_address="+15550000002"))
        db.add(CaseTypeModel(id=ids["ct"], name="Elsewhere Secret Process", version="1.0.0",
                             lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                             definition_json={"stages": []}, tenant_id=t.id))
        await db.commit()
        yield ids
    tenancy.clear_cache()


ROUTES = [
    ("/api/v1/user-directory?active_only=false", "elsewhere-user"),
    ("/api/v1/user-directory?active_only=false&tenant_id=elsewhere", "elsewhere-user"),
    ("/api/v1/escalation-trees?active_only=false", "Elsewhere tree"),
    ("/api/v1/escalation-trees?active_only=false&tenant_id=elsewhere", "Elsewhere tree"),
    ("/api/v1/hxbridge/connectors", "Elsewhere connector"),
    ("/api/v1/channels/conversations", "+15550000002"),
    ("/api/v1/rules/lint/report", "Elsewhere Secret Process"),
]


@pytest.mark.parametrize("url,marker", ROUTES)
async def test_a_caller_with_no_tenant_does_not_see_another_tenant(api, other, url, marker):
    r = await api.get(url, headers=_no_claim_admin())
    assert r.status_code in (200, 403), (url, r.status_code, r.text)
    assert marker not in r.text, f"{url} leaked another tenant's row to a caller with no tenant"


async def test_pii_findings_are_scoped_for_a_caller_without_cross_tenant_rights(api, other):
    """`security` reads PII findings but does not hold `compliance.crosstenant.read`.
    (`admin` does hold it, by the seeded role table — see the audit note in
    `Oct_TODO.md`; that is a policy question, not this bug.) The same branch
    scopes `/compliance/dr/backups` for a data-granted reader."""
    r = await api.get("/api/v1/compliance/privacy/findings",
                      headers=_no_claim_admin(roles=("security",)))
    assert r.status_code == 200, r.text
    assert "elsewhere.ssn" not in r.text


async def test_nor_open_another_tenants_conversation_by_id(api, other):
    r = await api.get(f"/api/v1/channels/conversations/{other['conv']}",
                      headers=_no_claim_admin())
    assert r.status_code == 404, r.text


@pytest.mark.parametrize("url,marker", [
    ("/api/v1/user-directory?active_only=false", "elsewhere-user"),
    ("/api/v1/hxbridge/connectors", "Elsewhere connector"),
    ("/api/v1/channels/conversations", "+15550000002"),
])
async def test_a_superadmin_still_sees_every_tenant(api, other, url, marker):
    r = await api.get(url, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert marker in r.text


async def test_a_tenant_admin_does_not_read_another_tenants_process_inventory(api, other):
    """The rules report's scoping branch was unreachable: it scoped only callers
    without `design.rules.read`, and the route refuses exactly those. Every
    tenant's admin read every tenant's case-type and rule names."""
    from velaris_shared.db.models import UserDirectoryModel

    async with TestSessionFactory() as db:
        db.add(TenantModel(id=uuid.uuid4(), slug="mine", name="Mine"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mine-admin", tenant_id="mine"))
        await db.commit()
    tenancy.clear_cache()
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(user_id=str(uuid.uuid4()), username="mine-admin", roles=["admin"],
                           secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    r = await api.get("/api/v1/rules/lint/report", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert "Elsewhere Secret Process" not in r.text



async def test_a_tenant_admin_reads_only_its_own_pii_findings_and_backups(api, other):
    """`admin` seeds to a wildcard privilege, so `compliance.crosstenant.read` could
    never keep a tenant admin out: every tenant's admin read every tenant's PII
    findings and backup records. Cross-tenant is superadmin-only (2026-09-17), and
    a platform backup (no tenant) is superadmin-only too."""
    from velaris_shared.db.models import UserDirectoryModel

    async with TestSessionFactory() as db:
        db.add(TenantModel(id=uuid.uuid4(), slug="mine", name="Mine"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mine-admin", tenant_id="mine"))
        await db.execute(insert(T["backup_records"]).values(
            id=uuid.uuid4(), location="s3://platform/full-install", tenant_id=None))
        await db.execute(insert(T["backup_records"]).values(
            id=uuid.uuid4(), location="s3://mine/backup", tenant_id="mine"))
        await db.commit()
    tenancy.clear_cache()
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    tok = create_dev_token(user_id=str(uuid.uuid4()), username="mine-admin", roles=["admin"],
                           secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    h = {"Authorization": f"Bearer {tok}"}
    r = await api.get("/api/v1/compliance/privacy/findings", headers=h)
    assert r.status_code == 200 and "elsewhere.ssn" not in r.text
    r = await api.get("/api/v1/compliance/dr/backups", headers=h)
    assert r.status_code == 200, r.text
    assert "s3://mine/backup" in r.text
    assert "s3://elsewhere/backup" not in r.text
    assert "s3://platform/full-install" not in r.text

    r = await api.get("/api/v1/compliance/dr/backups", headers=ADMIN_HEADERS)
    assert "s3://elsewhere/backup" in r.text and "s3://platform/full-install" in r.text
    r = await api.get("/api/v1/compliance/privacy/findings", headers=ADMIN_HEADERS)
    assert "elsewhere.ssn" in r.text
