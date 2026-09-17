"""Audit streaming to a SIEM — the cursor, the refusals, and what is not sent.

The two things worth testing hardest are not the happy path:

* **the cursor does not move when a push fails** — that is the whole difference
  between at-least-once and "we lost the afternoon the SIEM was down"; and
* **what the payload does not contain** — audit egress is justified as metadata
  about acts, and stops being justified the moment it starts carrying content.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.audit_stream import forwarder
from case_service.audit_stream.format import (
    format_batch, hec_event, redact_details, severity_rank,
)
from velaris_shared.db.models import AuditStreamTargetModel, SecurityEventModel


# ── The payload ──────────────────────────────────────────────────────────────


class TestFormat:
    def test_a_batch_is_newline_delimited_objects_not_an_array(self):
        """HEC's own framing. An array is rejected by Splunk."""
        events = [
            {"id": uuid.uuid4(), "event_type": "auth.login", "severity": "info",
             "timestamp": datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)},
            {"id": uuid.uuid4(), "event_type": "auth.logout", "severity": "info",
             "timestamp": datetime(2026, 9, 9, 12, 1, tzinfo=timezone.utc)},
        ]
        body = format_batch(events, host="box")
        assert not body.startswith("[")
        lines = body.split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event"]["event_type"] == "auth.login"

    def test_the_time_is_the_events_own_not_the_moment_it_was_forwarded(self):
        """A backlog caught up an hour later must not land in the SIEM as an
        hour of events all happening at once."""
        moment = datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)
        out = hec_event({"id": uuid.uuid4(), "timestamp": moment}, host="b", source="s")
        assert out["time"] == moment.timestamp()

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Every datetime column in this codebase is UTC. A naive one read as
        local time is how a three-second lease once displayed as two hours."""
        naive = datetime(2026, 9, 9, 8, 30)
        aware = naive.replace(tzinfo=timezone.utc)
        assert hec_event({"timestamp": naive}, host="b", source="s")["time"] == aware.timestamp()

    @pytest.mark.parametrize("key", [
        "password", "token", "api_key", "authorization", "case_data", "note",
    ])
    def test_content_and_credentials_never_reach_the_siem(self, key):
        """A SIEM indexes what it receives. A payload logged 'for debugging'
        would be searchable in another system forever."""
        out = redact_details({key: "the actual value", "path": "/api/v1/cases"})
        assert out[key] == "[redacted]"
        assert out["path"] == "/api/v1/cases", "metadata must survive"

    def test_redaction_reaches_a_nested_payload(self):
        """A nested dict is exactly where a request body ends up."""
        out = redact_details({"outer": {"password": "x", "method": "POST"}})
        assert out["outer"]["password"] == "[redacted]"
        assert out["outer"]["method"] == "POST"

    def test_an_unknown_severity_does_not_sort_below_the_floor(self):
        """A name this platform has not seen is more likely to be new and
        interesting than to be noise, so it must not vanish under a filter."""
        assert severity_rank("something_new") == severity_rank("info")
        assert severity_rank("critical") > severity_rank("warning")


# ── The cursor ───────────────────────────────────────────────────────────────


def _target(**overrides) -> AuditStreamTargetModel:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=uuid.uuid4(), tenant_id=None, tenant_key="platform",
        name="SOC", kind="splunk_hec",
        endpoint_url="https://splunk.internal:8088/services/collector",
        token_encrypted=None, verify_tls=True, enabled=True,
        min_severity=None, event_types=None,
        cursor_timestamp=None, cursor_event_id=None,
        forwarded_count=0, created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    return AuditStreamTargetModel(**defaults)


async def _seed_events(session, count: int, *, severity: str = "info",
                       event_type: str = "auth.login") -> list[SecurityEventModel]:
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = []
    for i in range(count):
        row = SecurityEventModel(
            id=uuid.uuid4(), event_type=event_type, severity=severity,
            user_id=f"user{i}", outcome="success", details={"i": i},
            timestamp=base + timedelta(seconds=i),
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


class TestCursor:
    async def test_a_failed_push_does_not_move_the_cursor(self, session, monkeypatch):
        """The difference between at-least-once and losing the afternoon the
        SIEM was down."""
        await _seed_events(session, 3)
        target = _target()
        session.add(target)
        await session.flush()

        async def refuse(_target, _events):
            raise forwarder.AuditStreamError("HTTP 503: down for maintenance")

        monkeypatch.setattr(forwarder, "push", refuse)
        with pytest.raises(forwarder.AuditStreamError):
            await forwarder.forward_target(session, target)

        assert target.cursor_event_id is None
        assert target.forwarded_count == 0
        assert "503" in target.last_error

    async def test_a_successful_push_advances_it_and_the_next_run_sends_nothing(
            self, session, monkeypatch):
        await _seed_events(session, 3)
        target = _target()
        session.add(target)
        await session.flush()

        pushed: list[list] = []

        async def accept(_target, events):
            pushed.append(list(events))

        monkeypatch.setattr(forwarder, "push", accept)
        first = await forwarder.forward_target(session, target)
        assert first["sent"] == 3
        assert target.cursor_event_id is not None
        assert target.last_error is None

        second = await forwarder.forward_target(session, target)
        assert second["sent"] == 0, "a caught-up target must not replay"
        assert len(pushed) == 1

    async def test_events_after_the_cursor_are_picked_up_next_run(self, session, monkeypatch):
        await _seed_events(session, 2)
        target = _target()
        session.add(target)
        await session.flush()
        monkeypatch.setattr(forwarder, "push", lambda *_: _noop())
        await forwarder.forward_target(session, target)

        # Two more arrive after the cursor.
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        for i in range(2):
            session.add(SecurityEventModel(
                id=uuid.uuid4(), event_type="auth.login", severity="info",
                timestamp=later + timedelta(seconds=i), details={}))
        await session.flush()

        assert (await forwarder.forward_target(session, target))["sent"] == 2

    async def test_a_filtered_out_event_still_advances_the_cursor(self, session, monkeypatch):
        """Otherwise a run of low-severity events is read again on every run,
        forever, and the stream never reaches the interesting one behind them."""
        await _seed_events(session, 4, severity="debug")
        target = _target(min_severity="warning")
        session.add(target)
        await session.flush()
        monkeypatch.setattr(forwarder, "push", lambda *_: _noop())

        result = await forwarder.forward_target(session, target)
        assert result["sent"] == 0
        assert result["filtered_out"] == 4
        assert target.cursor_event_id is not None, (
            "the cursor must pass over events the filter dropped")

    async def test_the_event_type_filter_is_a_list_not_a_string_match(self, session, monkeypatch):
        await _seed_events(session, 2, event_type="auth.login")
        await _seed_events(session, 3, event_type="case.delete")
        target = _target(event_types=json.dumps(["case.delete"]))
        session.add(target)
        await session.flush()
        seen: list = []

        async def accept(_t, events):
            seen.extend(events)

        monkeypatch.setattr(forwarder, "push", accept)
        await forwarder.forward_target(session, target)
        assert {e["event_type"] for e in seen} == {"case.delete"}

    async def test_one_dead_target_does_not_stop_another(self, session, monkeypatch):
        """A single unreachable SIEM must not turn one team's outage into
        everybody's blind spot."""
        await _seed_events(session, 2)
        good = _target(name="good")
        bad = _target(name="bad")
        session.add_all([good, bad])
        await session.flush()

        async def sometimes(target, _events):
            if target.name == "bad":
                raise forwarder.AuditStreamError("connection refused")

        monkeypatch.setattr(forwarder, "push", sometimes)
        results = await forwarder.run_due_targets(session)
        by_name = {r["name"]: r for r in results}
        assert by_name["good"]["sent"] == 2
        assert "error" in by_name["bad"]


async def _noop():
    return None


# ── Through the API ──────────────────────────────────────────────────────────


class TestApi:
    BODY = {
        "name": "SOC",
        "endpoint_url": "https://splunk.internal:8088/services/collector",
        "token": "a-real-hec-token",
        "acknowledge_platform_scope": True,
    }

    async def test_a_target_is_created_disabled(self, client):
        resp = await client.post("/api/v1/audit-stream/targets", json=self.BODY)
        assert resp.status_code == 201
        body = resp.json()
        assert body["enabled"] is False, (
            "an endpoint that starts receiving security events the moment "
            "somebody pastes a URL is a decision nobody made yet")
        assert body["has_token"] is True

    async def test_the_token_is_never_returned(self, client):
        created = await client.post("/api/v1/audit-stream/targets", json=self.BODY)
        assert "token" not in created.json()
        listed = await client.get("/api/v1/audit-stream/targets")
        assert "a-real-hec-token" not in listed.text
        assert listed.json()["items"][0]["has_token"] is True

    async def test_the_platform_scope_must_be_acknowledged(self, client):
        body = dict(self.BODY, name="Unacknowledged")
        body.pop("acknowledge_platform_scope")
        resp = await client.post("/api/v1/audit-stream/targets", json=body)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VEL-AUDIT-STREAM-SCOPE-400"

    @pytest.mark.parametrize("url,reason", [
        ("http://splunk.example.com:8088/x", "plain http to a remote host"),
        ("ftp://splunk/x", "not http"),
        ("https://user:pass@splunk/x", "credentials in the URL"),
        ("https:///x", "no host"),
    ])
    async def test_the_endpoint_is_validated(self, client, url, reason):
        resp = await client.post("/api/v1/audit-stream/targets",
                                 json=dict(self.BODY, name=reason, endpoint_url=url))
        assert resp.status_code == 400, reason
        assert resp.json()["error"]["code"] == "VEL-AUDIT-STREAM-URL-400"

    async def test_a_private_address_is_allowed_unlike_a_calendar_feed(self, client):
        """A SIEM lives inside the estate. Refusing 10.0.0.0/8 here would refuse
        the normal case; the privilege is the control instead."""
        resp = await client.post("/api/v1/audit-stream/targets", json=dict(
            self.BODY, name="Internal", endpoint_url="https://10.1.2.3:8088/services/collector"))
        assert resp.status_code == 201

    async def test_a_target_with_no_token_cannot_be_enabled(self, client):
        body = dict(self.BODY, name="Tokenless")
        body.pop("token")
        created = await client.post("/api/v1/audit-stream/targets", json=body)
        target_id = created.json()["id"]
        resp = await client.patch(f"/api/v1/audit-stream/targets/{target_id}",
                                  json={"enabled": True})
        assert resp.status_code == 409, (
            "a stream that is enabled and silent looks exactly like a quiet week")
        assert resp.json()["error"]["code"] == "VEL-AUDIT-STREAM-409"

    async def test_an_unknown_transport_is_refused_by_name(self, client):
        resp = await client.post("/api/v1/audit-stream/targets",
                                 json=dict(self.BODY, name="CEF", kind="syslog_cef"))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VEL-AUDIT-STREAM-KIND-400"

    async def test_a_new_target_starts_at_now_not_at_the_beginning_of_time(self, client):
        """Enabling one on an install with two years of history would otherwise
        ship two years of events as this morning's backlog."""
        created = await client.post("/api/v1/audit-stream/targets",
                                    json=dict(self.BODY, name="Fresh"))
        assert created.json()["cursor_timestamp"] is not None

    async def test_a_duplicate_name_is_refused(self, client):
        await client.post("/api/v1/audit-stream/targets", json=dict(self.BODY, name="Dup"))
        resp = await client.post("/api/v1/audit-stream/targets", json=dict(self.BODY, name="Dup"))
        assert resp.status_code == 409

    async def test_running_a_disabled_target_is_refused(self, client):
        created = await client.post("/api/v1/audit-stream/targets",
                                    json=dict(self.BODY, name="Off"))
        resp = await client.post(
            f"/api/v1/audit-stream/targets/{created.json()['id']}/run")
        assert resp.status_code == 409

    async def test_the_scheduler_offers_the_action(self, client):
        from case_service.scheduler import actions
        assert "audit_stream" in actions.kinds()


class TestTheAuditFoundThese:
    """Found by asking "is it 100% wired and 100% secure?" — 2026-09-09."""

    async def test_the_creator_is_recorded(self, client):
        """`created_by` read `user.id`, which `AuthenticatedUser` does not have
        — it is `user_id`. So the column was silently NULL on every target, and
        "who pointed our audit at that endpoint" had no answer."""
        from sqlalchemy import select

        from velaris_shared.db.models import AuditStreamTargetModel

        resp = await client.post("/api/v1/audit-stream/targets", json={
            "name": "Attribution",
            "endpoint_url": "https://splunk.internal:8088/services/collector",
            "token": "t", "acknowledge_platform_scope": True,
        })
        assert resp.status_code == 201
        from tests.conftest import TestSessionFactory
        async with TestSessionFactory() as s:
            row = (await s.execute(
                select(AuditStreamTargetModel).where(
                    AuditStreamTargetModel.name == "Attribution")
            )).scalars().first()
        assert row.created_by, "a target with no author cannot be questioned"

    def test_the_interactive_run_is_bounded_far_below_the_scheduled_one(self):
        """`forward_target` drains up to 20 batches, each with a 15s push
        timeout — five minutes inside an HTTP request. The scheduler can afford
        that; a request cannot."""
        import inspect

        from case_service.api.routers import audit_stream

        source = inspect.getsource(audit_stream.run_target)
        assert "max_batches=" in source, (
            "the interactive run must bound itself, or one click holds a worker "
            "for minutes")

    def test_the_scheduler_action_reports_through_run_not_validate(self):
        """`scheduler/service.py` discards `validate`'s return value, so a
        warning returned there reaches nobody. The observation belongs in
        `run`, whose result is recorded."""
        import inspect

        from case_service.scheduler.actions import AuditStreamAction

        assert "return None" in inspect.getsource(AuditStreamAction.validate)
        assert "note" in inspect.getsource(AuditStreamAction.run)


class TestQueryStringsCarryCredentials:
    """The audit middleware records `request.url.query` VERBATIM, and this
    platform really does put capabilities there: `GET /documents/{id}?grant=…`
    is a document access token (`api/routers/documents.py`), and an OAuth or
    SAML callback carries `?code=` and `?state=`.

    Forwarding those would put a live credential into another system's search
    index — retained on that system's terms, read by people who are not
    administrators here. Found by auditing the forwarder, not by a test failing.
    """

    @pytest.mark.parametrize("param", ["grant", "code", "token", "state", "otp"])
    def test_a_credential_in_the_query_string_does_not_travel(self, param):
        from case_service.audit_stream.format import scrub_query

        out = scrub_query(f"{param}=LIVE-SECRET-VALUE&page=2")
        assert "LIVE-SECRET-VALUE" not in out
        assert param in out, "the parameter NAME is metadata a SOC wants"
        assert "page=2" in out, "ordinary parameters must survive"

    def test_it_reaches_the_event_a_real_middleware_writes(self):
        """The middleware's payload is `{method, path, status, latency_ms,
        query}` — this is that shape, not a hand-made one."""
        event = hec_event({
            "id": uuid.uuid4(),
            "event_type": "api.get",
            "timestamp": datetime.now(timezone.utc),
            "details": {"method": "GET", "path": "/api/v1/documents/{id}",
                        "status": 200, "query": "grant=CAPABILITY-TOKEN"},
        }, host="box", source="velaris")
        rendered = json.dumps(event)
        assert "CAPABILITY-TOKEN" not in rendered
        assert "/api/v1/documents/{id}" in rendered

    def test_an_unparseable_query_is_dropped_rather_than_guessed(self):
        from case_service.audit_stream.format import scrub_query

        assert scrub_query("") == ""
        assert scrub_query(None) is None
