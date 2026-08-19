"""HxDraft Phase G — error catalog codes.

HxDraft's refusals are unusually easy to misread. A draft is generated, shown on
a card, and applied later — so at Apply the payload is *already known to have
been valid once*. A bare 400 there invites the user to press Apply again with the
identical body, which can never work. These tests pin the codes that say which
of the two things actually happened: the server disagreed with the draft
(`VALIDATION`), or the artifact moved underneath it (`STALE`).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.core import error_codes
from case_service.db.models import CaseTypeModel, RuleDefinitionModel


def _code(resp) -> str | None:
    body = resp.json()
    return (body.get("error") or {}).get("code")


def _headers(*roles: str) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings
    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="draft-tester", roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


# ── kind ──────────────────────────────────────────────────────────

class TestKind:
    @pytest.mark.asyncio
    async def test_unknown_kind_on_create(self, client):
        r = await client.post("/api/v1/hxnexus/draft",
                              json={"kind": "spaceship", "description": "make one"})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_KIND_400

    @pytest.mark.asyncio
    async def test_a_kind_that_exists_but_is_not_modifiable(self, client):
        """`sla` is a real kind, but modify drafts do not support it — same code,
        because the fix is the same: pick a kind this operation supports."""
        r = await client.post("/api/v1/hxnexus/draft",
                              json={"kind": "sla", "description": "x",
                                    "target_id": str(uuid.uuid4())})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_KIND_400

    @pytest.mark.asyncio
    async def test_unknown_kind_on_apply(self, client):
        r = await client.post("/api/v1/hxnexus/draft/apply",
                              json={"kind": "spaceship", "draft": {}})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_KIND_400

    @pytest.mark.asyncio
    async def test_only_rule_and_escalation_can_be_staged(self, client):
        r = await client.post("/api/v1/hxnexus/draft/stage",
                              json={"kind": "form", "draft": {"name": "f"}})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_KIND_400


# ── target lookup ─────────────────────────────────────────────────

class TestTargetLookup:
    @pytest.mark.asyncio
    async def test_malformed_id_is_not_the_same_as_absent(self, client):
        """A non-UUID reveals nothing about what exists, so it keeps its own
        code — splitting these is not an enumeration oracle."""
        r = await client.post("/api/v1/hxnexus/draft",
                              json={"kind": "rule", "description": "x",
                                    "target_id": "not-a-uuid"})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_BADID_400

    @pytest.mark.asyncio
    async def test_absent_target_is_notfound(self, client):
        r = await client.post("/api/v1/hxnexus/draft",
                              json={"kind": "rule", "description": "x",
                                    "target_id": str(uuid.uuid4())})
        assert r.status_code == 404
        assert _code(r) == error_codes.DRAFT_NOTFOUND_404

    @pytest.mark.asyncio
    async def test_one_notfound_code_covers_every_entity(self, client):
        """Rule, form, connector, case type and escalation tree share it — the
        entity is already named in `detail` and the fix is identical."""
        for kind in ("rule", "form", "connector", "escalation", "case_type"):
            r = await client.post("/api/v1/hxnexus/draft",
                                  json={"kind": kind, "description": "x",
                                        "target_id": str(uuid.uuid4())})
            assert r.status_code == 404, kind
            assert _code(r) == error_codes.DRAFT_NOTFOUND_404, kind


# ── validation vs staleness — the pair that matters ───────────────

class TestValidationAndStaleness:
    @pytest.mark.asyncio
    async def test_apply_rejects_an_invalid_draft_with_the_validation_code(self, client):
        r = await client.post("/api/v1/hxnexus/draft/apply",
                              json={"kind": "form", "draft": {"name": "", "definition_json": {}}})
        assert r.status_code == 400
        assert _code(r) == error_codes.DRAFT_VALIDATION_400
        assert "Draft failed validation" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_changed_target_is_stale_not_invalid(self, client, session):
        """The distinction the family exists for: this draft is well-formed. It
        is refused because the rule moved under it, and the fix is to re-draft —
        not to correct anything on the card."""
        rule = RuleDefinitionModel(
            name="Stale target", version="1.0.0", rule_type="when",
            scope="global", definition_json={"conditions": [], "actions": []},
            enabled=True, priority=0,
        )
        session.add(rule)
        await session.commit()

        r = await client.post(
            "/api/v1/hxnexus/draft/apply",
            headers=_headers("admin"),
            json={"kind": "rule",
                  # `mode` lives INSIDE draft — that is what routes apply to the
                  # modify path rather than the create path.
                  "draft": {"mode": "modify", "target_id": str(rule.id),
                            "base_checksum": "a-checksum-from-before-the-change",
                            "definition_json": {"conditions": [], "actions": []}}},
        )
        assert r.status_code == 409
        assert _code(r) == error_codes.DRAFT_STALE_409

    @pytest.mark.asyncio
    async def test_a_vanished_sla_policy_is_also_staleness(self, client, session):
        """Not a separate code: the draft was built against a policy that is
        gone, which is the same class of failure as a checksum mismatch."""
        ct = CaseTypeModel(
            name="SLA stale", version="1.0.0", default_priority="medium",
            definition_json={"stages": [], "sla_policies": []},
        )
        session.add(ct)
        await session.commit()

        from case_service.api.routers.hxdraft import _checksum
        r = await client.post(
            "/api/v1/hxnexus/draft/apply",
            headers=_headers("admin"),
            json={"kind": "sla",
                  "draft": {"case_type_id": str(ct.id),
                            "base_checksum": _checksum(ct.definition_json),
                            "replaces_policy_id": "policy-that-is-gone",
                            "policy": {"id": "policy-that-is-gone"}}},
        )
        assert r.status_code == 409
        assert _code(r) == error_codes.DRAFT_STALE_409


# ── role ──────────────────────────────────────────────────────────

class TestRole:
    @pytest.mark.asyncio
    async def test_applying_a_user_draft_without_admin(self, client):
        r = await client.post(
            "/api/v1/hxnexus/draft/apply",
            headers=_headers("viewer"),
            json={"kind": "user", "draft": {"username": "x", "email": "x@y.com"}},
        )
        assert r.status_code == 403
        assert _code(r) == error_codes.DRAFT_ROLE_403

    @pytest.mark.asyncio
    async def test_applying_a_rule_draft_without_designer(self, client):
        r = await client.post(
            "/api/v1/hxnexus/draft/apply",
            headers=_headers("viewer"),
            json={"kind": "rule", "draft": {"name": "r"}},
        )
        assert r.status_code == 403
        assert _code(r) == error_codes.DRAFT_ROLE_403

    @pytest.mark.asyncio
    async def test_staging_needs_no_role(self, client):
        """Deliberate: nothing goes live without a reviewer, so Stage is the
        propose-then-review path for people who cannot apply. A ROLE code here
        would be wrong."""
        r = await client.post(
            "/api/v1/hxnexus/draft/stage",
            headers=_headers("viewer"),
            json={"kind": "rule", "draft": {"name": "", "definition_json": {}}},
        )
        assert r.status_code != 403


# ── collisions ────────────────────────────────────────────────────

class TestExists:
    @pytest.mark.asyncio
    async def test_case_type_name_collision(self, client, session):
        ct = CaseTypeModel(name="Taken Name", version="1.0.0",
                           default_priority="medium",
                           definition_json={"stages": [{"id": "s1", "name": "S"}]})
        session.add(ct)
        await session.commit()

        r = await client.post(
            "/api/v1/hxnexus/draft/apply",
            headers=_headers("admin"),
            json={"kind": "case_type",
                  "draft": {"name": "Taken Name", "version": "1.0.0",
                            "definition_json": {"stages": [{"id": "s1", "name": "S"}]}}},
        )
        assert r.status_code == 409
        assert _code(r) == error_codes.DRAFT_EXISTS_409


# ── registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_draft_code_is_registered(self):
        drafts = {v for k, v in vars(error_codes).items()
                  if k.startswith("DRAFT_") and isinstance(v, str)}
        assert drafts, "no DRAFT_* constants found"
        assert drafts <= error_codes.KNOWN_CODES

    def test_the_router_raises_no_unregistered_code(self):
        import re
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / "case_service" / "api" / "routers" / "hxdraft.py"
        used = set(re.findall(r"coded\(\s*\d+,\s*([A-Z_0-9]+)", src.read_text()))
        for name in used:
            assert getattr(error_codes, name) in error_codes.KNOWN_CODES, name

    def test_generation_failures_are_split_by_resolution(self):
        """These shared one raise site behind a ternary status. Same message,
        opposite fixes — rephrase the description vs wait for the backend — so
        collapsing them again would send users to the wrong action."""
        assert error_codes.DRAFT_AI_503 != error_codes.DRAFT_GENERATE_400
        assert error_codes.DRAFT_AI_503.endswith("-503")
        assert error_codes.DRAFT_GENERATE_400.endswith("-400")
