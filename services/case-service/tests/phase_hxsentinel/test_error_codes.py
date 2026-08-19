"""HxSentinel — error catalog codes.

Three codes for 26 raise sites, and the restraint is the point: 15 are a missing
entity already named in `detail`, so splitting them by entity would grow the
registry without telling a reader anything. What *is* worth separating is the
SSRF refusal — the URL is well-formed and the manifest may be perfect, so an
operator sent to check their manifest will never find the problem.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.core import error_codes

RISK = "/api/v1/risk"


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.9.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


def _code(resp) -> str | None:
    return (resp.json().get("error") or {}).get("code")


class TestNotFound:
    @pytest.mark.asyncio
    async def test_absent_entities_share_one_code(self, client):
        """Finding, feedback, proposal, hold, model version, pack — the entity
        is in `detail` and the fix is identical, so one code covers them."""
        calls = [
            ("post", f"{RISK}/findings/{uuid.uuid4()}/feedback",
             {"score": 3, "note": "n"}),
            ("post", f"{RISK}/feedback/{uuid.uuid4()}/dispute", {"note": "n"}),
            ("post", f"{RISK}/holds/{uuid.uuid4()}/resolve",
             {"status": "cleared", "note": "n"}),
            ("post", f"{RISK}/rules/proposals/{uuid.uuid4()}/decision",
             {"decision": "approved", "note": "n"}),
            ("post", f"{RISK}/model/999999/activate", {}),
        ]
        seen = 0
        for method, path, body in calls:
            r = await getattr(client, method)(path, json=body)
            if r.status_code == 404:
                assert _code(r) == error_codes.RISK_NOTFOUND_404, path
                seen += 1
        assert seen >= 3, f"only {seen} of 5 paths 404'd — did the router change shape?"

    @pytest.mark.asyncio
    async def test_an_unknown_rule_uses_the_same_code(self, client):
        r = await client.put(f"{RISK}/rules/definitely-not-a-rule",
                             json={"severity": "medium"})
        assert r.status_code == 404
        assert _code(r) == error_codes.RISK_NOTFOUND_404


class TestUrlGuard:
    @pytest.mark.asyncio
    async def test_an_ssrf_blocked_host_gets_its_own_code(self, client):
        """The distinction the third code exists for: this URL is syntactically
        fine and the pack behind it might be valid. The *host* is refused."""
        r = await client.post(f"{RISK}/packs/install-from-url",
                              json={"url": "http://127.0.0.1:8080/pack.json"})
        assert r.status_code == 400, r.text
        assert _code(r) == error_codes.RISK_URL_400
        assert "URL rejected" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_metadata_address_is_refused_too(self, client):
        r = await client.post(f"{RISK}/packs/install-from-url",
                              json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert r.status_code == 400
        assert _code(r) == error_codes.RISK_URL_400

    @pytest.mark.asyncio
    async def test_a_blocked_host_is_not_reported_as_a_manifest_problem(self, client):
        """Collapsing this into REJECTED-400 would send an operator to check a
        manifest that was never fetched."""
        r = await client.post(f"{RISK}/packs/install-from-url",
                              json={"url": "http://localhost/pack.json"})
        assert _code(r) != error_codes.RISK_REJECTED_400


class TestRegistry:
    def test_every_risk_code_is_registered(self):
        codes = {v for k, v in vars(error_codes).items()
                 if k.startswith("RISK_") and isinstance(v, str)}
        assert codes, "no RISK_* constants found"
        assert codes <= error_codes.KNOWN_CODES

    def test_the_router_raises_no_unregistered_code(self):
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "sentinel.py").read_text()
        used = set(re.findall(r"coded\(\s*\d+,\s*([A-Z_0-9]+)", src))
        assert used
        for name in used:
            assert getattr(error_codes, name) in error_codes.KNOWN_CODES, name

    def test_the_restraint_is_deliberate(self):
        """22 of 26 sites coded with only three codes. If someone later adds a
        fourth per-entity NOTFOUND, this is the reminder that the entity is
        already in the message and the resolution does not differ."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "sentinel.py").read_text()
        assert src.count("raise coded(") == 22
        assert src.count("RISK_NOTFOUND_404") == 15 + 1   # raises + the import
