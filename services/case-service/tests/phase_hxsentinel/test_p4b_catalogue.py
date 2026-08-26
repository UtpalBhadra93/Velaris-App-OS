"""HxSentinel P4b — the bundled official signal-pack catalogue + one-click install.

Pins: the bundled packs validate CONFORMING at import, the catalogue lists them
with an installed flag, one-click install activates a bundled pack (and its
rules actually fire), and an unknown official id 404s.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from velaris_shared.db.models import CaseRiskSignalModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import official_packs, packs
from tests.conftest import create_case, deploy_case_type

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.8.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


class TestCatalogue:
    def test_all_bundled_packs_validate(self):
        # Import-time validation already ran; assert each still validates + is safe.
        for m in official_packs.OFFICIAL_PACKS:
            packs.validate_manifest(m)          # raises if unsafe/malformed
        ids = {c["pack_id"] for c in official_packs.catalogue()}
        assert {"generic-fraud", "telco-fraud", "lending"} <= ids

    async def test_catalogue_endpoint_flags_installed(self, client):
        r = await client.get(f"{RISK}/packs/catalogue")
        assert r.status_code == 200, r.text
        cat = {c["pack_id"]: c for c in r.json()["catalogue"]}
        assert cat["telco-fraud"]["installed"] is False
        await client.post(f"{RISK}/packs/catalogue/telco-fraud/install")
        cat2 = {c["pack_id"]: c for c in (await client.get(f"{RISK}/packs/catalogue")).json()["catalogue"]}
        assert cat2["telco-fraud"]["installed"] is True

    async def test_one_click_install_activates_rules(self, session, client):
        r = await client.post(f"{RISK}/packs/catalogue/generic-fraud/install")
        assert r.status_code == 201, r.text
        assert r.json()["pack_id"] == "generic-fraud"
        # generic-fraud has identity_velocity_high: phone_velocity >= 4
        ct = await deploy_case_type(client, name=f"C {uuid.uuid4().hex[:6]}")
        ca = uuid.UUID((await create_case(client, ct["id"]))["id"])
        for cid in (ca, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()):   # velocity == 4
            session.add(CaseRiskSignalModel(tenant_id="default", case_id=cid,
                        signal_type="identity_phone", signal_key="cat-1", hashed=True, value={}))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert "pack:generic-fraud:identity_velocity_high" in {f.rule_key for f in active}

    async def test_unknown_official_pack_404(self, client):
        assert (await client.post(f"{RISK}/packs/catalogue/nope/install")).status_code == 404

    async def test_official_install_is_reinstallable_idempotent(self, client):
        a = await client.post(f"{RISK}/packs/catalogue/lending/install")
        b = await client.post(f"{RISK}/packs/catalogue/lending/install")
        assert a.status_code == 201 and b.status_code == 201   # upsert, no duplicate error
        packs_ = (await client.get(f"{RISK}/packs")).json()["packs"]
        assert sum(1 for p in packs_ if p["pack_id"] == "lending") == 1
