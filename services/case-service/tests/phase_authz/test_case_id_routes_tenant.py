"""Every route with a `{case_id}` refuses another tenant's case — 2026-09-29.

`test_ungated_routes.py` finds routes with NO identity. It cannot see a route
that authenticates the caller and then never asks whose case it was handed,
and on 2026-09-29 that shape was found on 31 routes: all of `/payments`, the
SLA start/pause/resume routes, KYC and e-sign, CRM and invoice sync, document
extraction, lineage, the HxStream replay and case variables. Proven by the
first test below before the fix — an admin in another tenant got a 2xx from 15
of them, including `/variables/cases/{id}` returning the case's data.

Two checks, because each is blind where the other sees:

* **the probe** calls every `{case_id}` route as another tenant's admin. It
  cannot get past a 422, so a write whose body it cannot guess proves nothing;
* **the static check** reads every `{case_id}` handler for a gate. It cannot
  tell a gate that works from one that does not — the probe can.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import re
import uuid

from fastapi.routing import APIRoute

from case_service.main import app
from tests.test_payments_authz import _headers, api, world  # noqa: F401

#: Answer 2xx to another tenant, and are right to.
CROSS_TENANT_2XX_OK = {
    # Releases a step lock only where `locked_by` is the CALLER — there is
    # nothing of the other tenant's to touch.
    ("POST", "/api/v1/cases/{case_id}/steps/{step_id}/unlock"),
    # Filtered to the caller's tenant in the query: the answer is empty.
    ("GET", "/api/v1/costing/cases/{case_id}/cost"),
    ("GET", "/api/v1/channels/cases/{case_id}/conversations"),
}

#: A gate, by name. Helpers that wrap `require_case` are listed so a rename of
#: one shows up here rather than silently passing.
GATE = re.compile(
    r"require_case|_authorized_case|_assert_case_access|_case_gate|_reach_case|"
    r"_stage_move_gate|same_tenant|case_visible_to|_scoped\(|tenant_id\s*==|"
    r"_get_run\(|_manifest_allows_cases|locked_by == user|"
    r"_get_case_or_404\([^)]*user|_tenant_uuid_for_costing")

#: Portal routes authenticate a CUSTOMER and match the case to them — a
#: different model, pinned by the portal's own tests. The broker authenticates
#: a package grant.
OTHER_MODEL = ("/api/v1/portal/", "/api/v1/broker/")


def _case_routes():
    for r in app.routes:
        if isinstance(r, APIRoute) and "{case_id}" in r.path:
            yield r


async def test_no_case_id_route_answers_another_tenant(api, world):
    cid = str(world["case"].id)
    h = _headers("mallory", ["admin"])
    leaks = []
    for r in _case_routes():
        if r.path.startswith(OTHER_MODEL):
            continue
        path = r.path.replace("{case_id}", cid)
        path = re.sub(r"\{[^}:]+(:path)?\}", str(uuid.uuid4()), path)
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            if (method, r.path) in CROSS_TENANT_2XX_OK:
                continue
            resp = await api.request(method, path, headers=h, json={})
            if resp.status_code < 300:
                leaks.append((method, r.path, resp.status_code))
    assert leaks == [], leaks


def test_every_case_id_handler_has_a_gate():
    ungated = []
    for r in _case_routes():
        if r.path.startswith(OTHER_MODEL):
            continue
        if not GATE.search(inspect.getsource(r.endpoint)):
            ungated.append((sorted(r.methods), r.path))
    assert ungated == [], ungated


# ── the global lists beside those routes ─────────────────────────────────────

_LISTS = {
    "/api/v1/identity/verifications": "IdentityVerificationModel",
    "/api/v1/esign/requests": "ESignRequestModel",
    "/api/v1/crm/records": "CrmSyncRecordModel",
    "/api/v1/invoices/records": "InvoiceRecordModel",
    "/api/v1/docintel/extractions": "DocExtractionJobModel",
    "/api/v1/docintel/storage": "DocStorageRouteModel",
    "/api/v1/payments/requests": None,
    "/api/v1/payments/disbursements": None,
}


async def test_the_global_lists_hold_only_the_callers_tenant(api, world):
    """Each of these returned every tenant's rows until 2026-09-29."""
    from velaris_shared.db import models
    from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]

    async with TestSessionFactory() as db:
        for name in filter(None, _LISTS.values()):
            extra = {"document_name": "a.pdf"} if name == "DocStorageRouteModel" else {}
            db.add(getattr(models, name)(tenant_id="acme", case_id=world["case"].id,
                                         step_id="pay", **extra))
        await db.commit()
    for path in _LISTS:
        mine = await api.get(path, headers=_headers("ann", ["admin"]))
        theirs = await api.get(path, headers=_headers("mallory", ["admin"]))
        assert mine.status_code == 200, (path, mine.text)
        assert len(mine.json()) >= 1, path
        assert theirs.status_code == 200 and theirs.json() == [], (path, theirs.text)
