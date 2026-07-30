"""HxSentinel E2 — community pack distribution (install from a remote URL).

Pins the safety boundary: SSRF-guarded (private/loopback hosts refused BEFORE
any request), size-capped, and the remote manifest gets the SAME CONFORMING
sandbox validation as a pasted one — no extra trust for being remote.

The HTTP fetch (`packs._fetch`) is patched so tests never hit the network; the
SSRF guard runs for real against loopback.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from case_service.sentinel import packs

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


def _manifest(when="phone_velocity >= 3", pack_id="community-1"):
    return {"id": pack_id, "name": "Community Pack", "version": "1.0.0",
            "rules": [{"key": "r1", "when": when, "severity": "medium",
                       "title": "T", "message": "M"}]}


class TestInstallFromUrl:
    async def test_ssrf_loopback_refused(self, client):
        # The SSRF guard runs for real — a loopback host is refused BEFORE any fetch.
        r = await client.post(f"{RISK}/packs/install-from-url",
                             json={"url": "http://127.0.0.1:9/x.json"})
        assert r.status_code == 400
        assert "rejected" in r.json()["detail"].lower()

    async def test_ssrf_private_host_refused(self, client):
        r = await client.post(f"{RISK}/packs/install-from-url",
                             json={"url": "http://169.254.169.254/latest/meta-data"})
        assert r.status_code == 400

    async def test_valid_remote_manifest_installs(self, client):
        with patch("case_service.sentinel.packs.validate_outbound_url", AsyncMock()), \
             patch("case_service.sentinel.packs._fetch",
                   AsyncMock(return_value=(200, json.dumps(_manifest()).encode()))):
            r = await client.post(f"{RISK}/packs/install-from-url",
                                 json={"url": "https://example.com/pack.json"})
        assert r.status_code == 201, r.text
        assert r.json()["pack_id"] == "community-1"

    async def test_remote_unsafe_manifest_rejected(self, client):
        with patch("case_service.sentinel.packs.validate_outbound_url", AsyncMock()), \
             patch("case_service.sentinel.packs._fetch",
                   AsyncMock(return_value=(200, json.dumps(_manifest(when="__import__('os')")).encode()))):
            r = await client.post(f"{RISK}/packs/install-from-url",
                                 json={"url": "https://example.com/evil.json"})
        assert r.status_code == 400          # sandbox rejects it, remote or not

    async def test_non_200_rejected(self, client):
        with patch("case_service.sentinel.packs.validate_outbound_url", AsyncMock()), \
             patch("case_service.sentinel.packs._fetch", AsyncMock(return_value=(404, b""))):
            r = await client.post(f"{RISK}/packs/install-from-url",
                                 json={"url": "https://example.com/missing.json"})
        assert r.status_code == 400

    async def test_oversize_manifest_rejected(self, client):
        big = b"x" * (packs._MANIFEST_MAX_BYTES + 1)
        with patch("case_service.sentinel.packs.validate_outbound_url", AsyncMock()), \
             patch("case_service.sentinel.packs._fetch", AsyncMock(return_value=(200, big))):
            r = await client.post(f"{RISK}/packs/install-from-url",
                                 json={"url": "https://example.com/big.json"})
        assert r.status_code == 400
