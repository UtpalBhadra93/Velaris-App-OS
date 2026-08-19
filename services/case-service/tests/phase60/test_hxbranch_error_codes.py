"""HxBranch — error catalog codes.

A branch is a state machine with an owner, a reviewer and a remote. Almost every
refusal answers one of four questions — what state is it in, who are you, is the
remote usable, did main move underneath it — and a bare 400 answers none of them.
These tests pin the four apart, and pin the two that an auditor will ask about:
separation of duties, and the review lock.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.core import error_codes
from case_service.db.models import ArtifactBranchModel, EnvironmentRegistryModel


def _code(resp) -> str | None:
    return (resp.json().get("error") or {}).get("code")


def _headers(user_id: str, roles: list[str] | None = None) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings
    s = get_settings()
    token = create_dev_token(
        user_id=user_id, username=f"u-{user_id[:8]}", roles=roles or ["designer"],
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def _make_branch(session, *, owner_id: str, status: str = "open",
                       reviewer_id: str | None = None,
                       artifact_type: str = "rule",
                       artifact_id: str | None = None) -> ArtifactBranchModel:
    b = ArtifactBranchModel(
        name=f"test/{uuid.uuid4().hex[:8]}", description="",
        branch_type="artifact", artifact_type=artifact_type,
        artifact_id=artifact_id or str(uuid.uuid4()),
        source_env_name="dev (local)", status=status,
        base_snapshot={"v": 1}, content_snapshot={"v": 2},
        created_by="owner", owner_id=owner_id,
        assigned_reviewer_id=reviewer_id,
    )
    session.add(b)
    await session.commit()
    await session.refresh(b)
    return b


# ── state machine ─────────────────────────────────────────────────

class TestState:
    @pytest.mark.asyncio
    async def test_submitting_a_merged_branch(self, client, session):
        owner = str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="merged")
        r = await client.post(f"/api/v1/branches/{b.id}/submit",
                              headers=_headers(owner),
                              json={"assigned_reviewer_id": str(uuid.uuid4())})
        assert r.status_code == 400
        assert _code(r) == error_codes.BRANCH_STATE_400
        assert "merged" in r.json()["detail"], "the current status must be named"

    @pytest.mark.asyncio
    async def test_recalling_a_branch_that_is_not_in_review(self, client, session):
        owner = str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="open")
        r = await client.post(f"/api/v1/branches/{b.id}/recall", headers=_headers(owner))
        assert r.status_code == 400
        assert _code(r) == error_codes.BRANCH_STATE_400

    @pytest.mark.asyncio
    async def test_reviewing_a_branch_that_is_not_pending(self, client, session):
        owner, reviewer = str(uuid.uuid4()), str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="open", reviewer_id=reviewer)
        r = await client.post(f"/api/v1/branches/{b.id}/reviews",
                              headers=_headers(reviewer),
                              json={"decision": "approved"})
        assert r.status_code == 400
        assert _code(r) == error_codes.BRANCH_STATE_400


# ── the review lock ───────────────────────────────────────────────

class TestLock:
    @pytest.mark.asyncio
    async def test_editing_content_under_review_is_locked_not_invalid(self, client, session):
        """423, not 400: a reviewer is looking at a specific diff, and editing
        underneath them would mean approving something nobody read. The status
        code is the difference between 'fix your request' and 'recall first'."""
        owner = str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="pending_review")
        r = await client.patch(f"/api/v1/branches/{b.id}/content",
                               headers=_headers(owner),
                               json={"content_snapshot": {"v": 3}})
        assert r.status_code == 423
        assert _code(r) == error_codes.BRANCH_LOCKED_423
        assert "recall" in r.json()["detail"].lower()


# ── actor ─────────────────────────────────────────────────────────

class TestActor:
    @pytest.mark.asyncio
    async def test_a_non_owner_cannot_edit_content(self, client, session):
        b = await _make_branch(session, owner_id=str(uuid.uuid4()), status="open")
        r = await client.patch(f"/api/v1/branches/{b.id}/content",
                               headers=_headers(str(uuid.uuid4())),
                               json={"content_snapshot": {"v": 3}})
        assert r.status_code == 403
        assert _code(r) == error_codes.BRANCH_ACTOR_403

    @pytest.mark.asyncio
    async def test_a_non_reviewer_cannot_post_a_decision(self, client, session):
        """Authorization is by relationship to THIS branch, not by role: being a
        designer, or even the owner, does not make you the assigned reviewer."""
        owner, reviewer = str(uuid.uuid4()), str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="pending_review",
                               reviewer_id=reviewer)
        r = await client.post(f"/api/v1/branches/{b.id}/reviews",
                              headers=_headers(owner),
                              json={"decision": "approved"})
        assert r.status_code == 403
        assert _code(r) == error_codes.BRANCH_ACTOR_403

    @pytest.mark.asyncio
    async def test_a_non_owner_cannot_recall(self, client, session):
        b = await _make_branch(session, owner_id=str(uuid.uuid4()), status="pending_review")
        r = await client.post(f"/api/v1/branches/{b.id}/recall",
                              headers=_headers(str(uuid.uuid4())))
        assert r.status_code == 403
        assert _code(r) == error_codes.BRANCH_ACTOR_403


# ── separation of duties ──────────────────────────────────────────

class TestSeparationOfDuties:
    @pytest.mark.asyncio
    async def test_self_review_is_refused_with_its_own_code(self, client, session):
        """The control the whole review flow exists for, so it does not share a
        code with ordinary validation — an auditor asks about this one by name."""
        owner = str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="open")
        r = await client.post(f"/api/v1/branches/{b.id}/submit",
                              headers=_headers(owner),
                              json={"assigned_reviewer_id": owner})
        assert r.status_code == 400
        assert _code(r) == error_codes.BRANCH_SOD_400

    @pytest.mark.asyncio
    async def test_a_different_reviewer_is_accepted(self, client, session):
        """The guard is narrow — it refuses self-review, not review."""
        owner = str(uuid.uuid4())
        b = await _make_branch(session, owner_id=owner, status="open")
        r = await client.post(f"/api/v1/branches/{b.id}/submit",
                              headers=_headers(owner),
                              json={"assigned_reviewer_id": str(uuid.uuid4())})
        assert r.status_code == 200, r.text


# ── lookup ────────────────────────────────────────────────────────

class TestNotFound:
    @pytest.mark.asyncio
    async def test_unknown_branch(self, client):
        r = await client.get(f"/api/v1/branches/{uuid.uuid4()}", headers=_headers("x"))
        assert r.status_code == 404
        assert _code(r) == error_codes.BRANCH_NOTFOUND_404


# ── remote environment ────────────────────────────────────────────

class TestEnvironment:
    @pytest.mark.asyncio
    async def test_an_environment_with_no_url_is_a_config_error(self, client, session):
        env = EnvironmentRegistryModel(tenant_id="default", name="staging",
                                       label="Staging", url=None)
        session.add(env)
        await session.commit()
        r = await client.get(f"/api/v1/branches/remote/{env.id}/available",
                             headers=_headers("x", ["admin"]))
        assert r.status_code == 400
        assert _code(r) in (error_codes.BRANCH_ENV_400, error_codes.BRANCH_ENVAUTH_400)

    @pytest.mark.asyncio
    async def test_missing_and_rejected_tokens_have_different_codes(self):
        """A missing token sends the operator to Connections to set one; a
        rejected token sends them to the remote to rotate one. Collapsing them
        points at a settings screen that already looks correctly filled in."""
        assert error_codes.BRANCH_ENV_400 != error_codes.BRANCH_ENVAUTH_400


# ── registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_branch_code_is_registered(self):
        codes = {v for k, v in vars(error_codes).items()
                 if k.startswith("BRANCH_") and isinstance(v, str)}
        assert codes, "no BRANCH_* constants found"
        assert codes <= error_codes.KNOWN_CODES

    def test_the_router_raises_no_unregistered_code(self):
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "case_service" / "api"
               / "routers" / "branches.py")
        used = set(re.findall(r"coded\(\s*\n?\s*\d+,\s*([A-Z_0-9]+)", src.read_text()))
        assert used, "no coded() calls found — did the router change shape?"
        for name in used:
            assert getattr(error_codes, name) in error_codes.KNOWN_CODES, name

    def test_the_merge_conflict_keeps_a_dedicated_code(self):
        """Shared shape with HxDraft's staleness guard, but a different subject:
        there the draft went stale, here main moved. Both must stay 409."""
        assert error_codes.BRANCH_CONFLICT_409.endswith("-409")
        assert error_codes.BRANCH_CONFLICT_409 != error_codes.DRAFT_STALE_409
