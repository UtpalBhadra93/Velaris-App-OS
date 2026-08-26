"""HxVault (#19) — per-tenant DEK envelope encryption.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from case_service import hxvault
from case_service.hxvault import crypto, keyring
from case_service.hxbridge.encryption import (
    encrypt_credentials,
    decrypt_credentials,
    mask_credentials,
)


@pytest_asyncio.fixture(autouse=True)
async def _isolate_vault():
    """DEK cache is process-global — clear it (and the KEK cache) each test."""
    keyring.clear_cache()
    crypto.reset_kek_cache()
    yield
    keyring.clear_cache()
    crypto.reset_kek_cache()


# ── Acceptance gate: round-trip for a real tenant AND None ────────────────────

@pytest.mark.asyncio
async def test_roundtrip_real_tenant(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"top-secret", "connector_credentials")
    assert ct.startswith("hxv2:")
    assert hxvault.decrypt(ct) == b"top-secret"


@pytest.mark.asyncio
async def test_roundtrip_platform_tenantless(session):
    await hxvault.ensure_dek(session, None)
    ct = hxvault.encrypt(None, b"tenantless-data", "case_data")
    assert ct.startswith("hxv2:-:")          # "-" marks the platform/None scope
    assert hxvault.decrypt(ct) == b"tenantless-data"


@pytest.mark.asyncio
async def test_two_tenants_get_distinct_keys(session):
    a, b = uuid.uuid4(), uuid.uuid4()
    await hxvault.ensure_dek(session, a)
    await hxvault.ensure_dek(session, b)
    ct_a = hxvault.encrypt(a, b"x", "ctx")
    # tenant b cannot have produced ct_a; decrypt still works because tenant is
    # self-described in the envelope and a's DEK is cached.
    assert hxvault.decrypt(ct_a) == b"x"
    assert keyring.cached_dek(a) != keyring.cached_dek(b)


# ── Crypto-shredding ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shred_makes_data_unrecoverable(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"gdpr", "ctx")
    assert await hxvault.shred(session, tid) is True
    # KeyShreddedError, NOT DekNotLoadedError: this read is unrecoverable, and the
    # two map to different HTTP codes (410 vs a transient 503 that invites a retry).
    with pytest.raises(keyring.KeyShreddedError):
        hxvault.decrypt(ct)
    # re-ensure after shred must refuse to resurrect the shredded key
    with pytest.raises(keyring.KeyShreddedError):
        await hxvault.ensure_dek(session, tid)


@pytest.mark.asyncio
async def test_shred_nonexistent_returns_false(session):
    assert await hxvault.shred(session, uuid.uuid4()) is False


# ── Tamper / wrong-key resistance ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aad_tamper_swap_tenant_fails(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    other = uuid.uuid4()
    await hxvault.ensure_dek(session, other)
    ct = hxvault.encrypt(tid, b"secret", "ctx")
    # rewrite the envelope's tenant field to another (cached) tenant → AAD mismatch
    parts = ct.split(":", 4)
    parts[1] = str(other)
    forged = ":".join(parts)
    with pytest.raises(Exception):
        hxvault.decrypt(forged)


@pytest.mark.asyncio
async def test_aad_tamper_swap_context_fails(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"secret", "ctx-a")
    import base64
    parts = ct.split(":", 4)
    parts[3] = base64.urlsafe_b64encode(b"ctx-b").decode().rstrip("=")
    with pytest.raises(Exception):
        hxvault.decrypt(":".join(parts))


@pytest.mark.asyncio
async def test_wrong_kek_cannot_unwrap(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    # Change the KEK and drop the cache → unwrap from DB must fail.
    keyring.clear_cache()
    crypto.reset_kek_cache()
    crypto._kek_cache = b"\x00" * 32  # force a wrong KEK
    with pytest.raises(Exception):
        await hxvault.ensure_dek(session, tid)


# ── Consumer: connector credentials (hxv2) + legacy hxv1 back-compat ──────────

@pytest.mark.asyncio
async def test_connector_creds_vault_roundtrip(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    creds = {"api_key": "sk_live_abc", "secret": "shh"}
    stored = encrypt_credentials(creds, tenant_id=tid, vault=True)
    assert stored["_enc"].startswith("hxv2:")
    assert decrypt_credentials(stored) == creds
    assert mask_credentials(stored) == {"_enc": "***"}


def test_legacy_hxv1_still_decrypts():
    """hxv1 is READ forever and written never (2026-08-24).

    The default (vault=False) path now writes hxv3 — AES-256-GCM instead of the
    hand-rolled SHA-256 keystream — so this can no longer produce an hxv1 value
    by calling the encryptor. It builds one the way the old code did, which is
    the only way to prove that secrets stored before the change still open.
    """
    import base64
    import json
    import os

    from case_service.hxbridge import encryption as _enc

    creds = {"token": "legacy"}
    key = _enc._get_key()
    nonce = os.urandom(16)
    payload = nonce + _enc._xor_encrypt(
        key, json.dumps(creds, sort_keys=True).encode(), nonce)
    stored = {"_enc": "hxv1:" + base64.urlsafe_b64encode(payload).decode()}
    assert decrypt_credentials(stored) == creds

    # And the write path has genuinely moved on.
    assert encrypt_credentials(creds)["_enc"].startswith("hxv3:")


def test_plain_dict_passthrough():
    assert decrypt_credentials({"user": "x"}) == {"user": "x"}
    assert decrypt_credentials({}) == {}


@pytest.mark.asyncio
async def test_non_uuid_tenant_falls_back_to_platform(session):
    # A legacy non-UUID tenant slug must NOT 500 — it maps to the platform DEK.
    await hxvault.ensure_dek(session, "t1")          # slug, not a UUID
    ct = hxvault.encrypt("t1", b"legacy", "ctx")
    assert ct.startswith("hxv2:-:")                   # "-" = platform scope
    assert hxvault.decrypt(ct) == b"legacy"


# ── Multi-worker coherence: resync_cache ──────────────────────────────────────

@pytest.mark.asyncio
async def test_resync_loads_dek_created_elsewhere(session):
    """Simulates a DEK created on another worker: this worker's cache misses, then
    resync pulls it in so the sync decrypt path stops raising."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)                    # worker A creates + stores
    ct = hxvault.encrypt(tid, b"cross-worker", "connector_credentials")

    keyring.clear_cache()                                     # worker B: cold cache
    with pytest.raises(hxvault.DekNotLoadedError):
        hxvault.decrypt(ct)

    out = await hxvault.resync_cache(session)                 # worker B reconciles
    assert out["added"] >= 1
    assert hxvault.decrypt(ct) == b"cross-worker"             # now decryptable


@pytest.mark.asyncio
async def test_resync_evicts_shredded_dek(session):
    """A DEK shredded on another worker must be evicted here so it stops decrypting."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    assert keyring.cached_dek(tid) is not None
    await hxvault.shred(session, tid)                         # shredded (DB row → shredded)
    keyring._DEK_CACHE[keyring._ckey(tid)] = b"stale-still-cached"  # simulate other worker's stale entry
    out = await hxvault.resync_cache(session)
    assert out["evicted"] >= 1
    assert keyring.cached_dek(tid) is None


# ── Shredded vs. merely-absent: the distinction the error codes rest on ───────

@pytest.mark.asyncio
async def test_cold_cache_is_transient_not_shredded(session):
    """A key that was never warmed must stay DekNotLoadedError.

    The counterpart to the shred test: if every cache miss reported "shredded",
    the 410 would be as useless as the 500 both codes replaced.
    """
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"cold", "ctx")
    keyring.clear_cache()

    assert keyring.is_shredded(tid) is False
    with pytest.raises(hxvault.DekNotLoadedError):
        hxvault.decrypt(ct)
    # ...and it must NOT be the shredded subclassless sibling
    try:
        hxvault.decrypt(ct)
    except Exception as e:  # noqa: BLE001
        assert not isinstance(e, keyring.KeyShreddedError)


@pytest.mark.asyncio
async def test_encrypt_after_shred_also_reports_shredded(session):
    """The write path shares the distinction — not just decrypt."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    await hxvault.shred(session, tid)
    with pytest.raises(keyring.KeyShreddedError):
        hxvault.encrypt(tid, b"nope", "ctx")


@pytest.mark.asyncio
async def test_warm_cache_learns_shredded_from_db(session):
    """A worker that starts *after* the shred still reports it correctly.

    `warm_cache` used to select only active rows, so a restarted worker lost all
    evidence of shredding and fell back to the transient error.
    """
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"gone", "ctx")
    await hxvault.shred(session, tid)

    keyring.clear_cache()                 # fresh process, knows nothing
    assert keyring.is_shredded(tid) is False
    await hxvault.warm_cache(session)     # startup

    assert keyring.is_shredded(tid) is True
    with pytest.raises(keyring.KeyShreddedError):
        hxvault.decrypt(ct)


@pytest.mark.asyncio
async def test_resync_learns_shredded_from_another_worker(session):
    """Shredded elsewhere → this worker converges to 410 within one interval."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"gone", "ctx")
    await hxvault.shred(session, tid)

    keyring._SHREDDED.clear()             # simulate: shred happened on worker A
    out = await hxvault.resync_cache(session)

    assert out["shredded"] >= 1
    assert keyring.is_shredded(tid) is True
    with pytest.raises(keyring.KeyShreddedError):
        hxvault.decrypt(ct)
