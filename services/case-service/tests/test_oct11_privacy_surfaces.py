"""Oct item 11 — HxPrivacy on three more surfaces.

  11a  the Database Manager's raw table export
  11e  text sent to an external AI provider
  11f  retention's `anonymize` reaching the case's free text

Each is its own switch and each is off unless set. The tests that matter are
the "off" ones: an install that changes nothing must see nothing change.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from velaris_shared.config import Settings, get_settings
from velaris_shared.db import pii_redaction as pii
from velaris_shared.db.models import (
    CaseInstanceModel, CaseMessageModel, ChannelConversationModel,
    ChannelEndpointModel, ChannelMessageModel,
    CopilotConversationModel, CopilotMessageModel, PIIFindingModel,
    RetentionPolicyModel,
)
from case_service.compliance import retention
from case_service.hxnexus.pseudonymizer import Pseudonymizer, add_confirmed_fields

SWITCHES = {
    "pii_redact_table_exports": "VELARIS_CASE_PII_REDACT_TABLE_EXPORTS",
    "pii_mask_ai_egress": "VELARIS_CASE_PII_MASK_AI_EGRESS",
    "retention_anonymize_case_text": "VELARIS_CASE_RETENTION_ANONYMIZE_CASE_TEXT",
}


def _settings(**over):
    return get_settings().model_copy(update=over)


async def _confirm(session, field_path, status="confirmed"):
    session.add(PIIFindingModel(
        resource_type="form", resource_id=uuid.uuid4(), field_path=field_path,
        category="name", confidence=0.9, status=status))
    await session.flush()


# ── the switches ──────────────────────────────────────────────────────

def test_all_three_are_off_as_shipped():
    s = get_settings()
    assert [getattr(s, name) for name in SWITCHES] == [False, False, False]


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "2", "enabled", "None"])
def test_an_unreadable_switch_is_off_and_does_not_stop_the_boot(monkeypatch, raw):
    for env in SWITCHES.values():
        monkeypatch.setenv(env, raw)
    s = Settings()
    assert [getattr(s, name) for name in SWITCHES] == [False, False, False]


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " on "])
def test_a_plain_yes_is_on(monkeypatch, raw):
    for env in SWITCHES.values():
        monkeypatch.setenv(env, raw)
    s = Settings()
    assert [getattr(s, name) for name in SWITCHES] == [True, True, True]


def test_the_existing_export_switch_does_not_imply_the_new_ones(monkeypatch):
    monkeypatch.setenv("VELARIS_CASE_PII_REDACT_EXPORTS", "true")
    s = Settings()
    assert s.pii_redact_exports is True
    assert [getattr(s, name) for name in SWITCHES] == [False, False, False]


# ── 11a: nested redaction ─────────────────────────────────────────────

PATHS = {"customer_name", "iban"}


def test_deep_redaction_reaches_inside_json_columns():
    rows = [{"id": 1, "status": "open",
             "data": {"customer_name": "Ann Smith", "amount": 10,
                      "payer": {"IBAN": "DE00 1234", "bank": "X"},
                      "people": [{"customer_name": "Bob"}]}}]
    out = pii.redact_rows_deep(rows, PATHS)
    data = out[0]["data"]
    assert data["customer_name"] == pii.REDACTED
    assert data["payer"] == {"IBAN": pii.REDACTED, "bank": "X"}
    assert data["people"] == [{"customer_name": pii.REDACTED}]
    assert (out[0]["id"], out[0]["status"], data["amount"]) == (1, "open", 10)
    assert rows[0]["data"]["customer_name"] == "Ann Smith", "input must not be mutated"


def test_deep_redaction_reads_json_that_arrives_as_text():
    raw = json.dumps({"customer_name": "Ann Smith", "amount": 10})
    out = pii.redact_rows_deep([{"data": raw}], PATHS)
    assert json.loads(out[0]["data"]) == {"customer_name": pii.REDACTED, "amount": 10}


def test_deep_redaction_leaves_untouched_text_byte_for_byte():
    raw = '{"amount":   10}'            # odd spacing must survive
    rows = [{"data": raw, "note": "{not json", "customer_name": "Ann"}]
    out = pii.redact_rows_deep(rows, PATHS)
    assert out[0]["data"] == raw and out[0]["note"] == "{not json"
    assert out[0]["customer_name"] == pii.REDACTED


def test_deep_redaction_keeps_every_key():
    rows = [{"a": 1, "customer_name": "x", "data": {"iban": "y", "z": None}}]
    out = pii.redact_rows_deep(rows, PATHS)
    assert list(out[0]) == ["a", "customer_name", "data"]
    assert list(out[0]["data"]) == ["iban", "z"]


@pytest.mark.parametrize("rows,paths", [([], PATHS), ([{"customer_name": "x"}], set())])
def test_deep_redaction_with_nothing_to_do_returns_the_input(rows, paths):
    assert pii.redact_rows_deep(rows, paths) is rows


@pytest.mark.asyncio
async def test_only_confirmed_findings_redact(session):
    await _confirm(session, "sections[0].fields[1].customer_name")
    await _confirm(session, "guess_field", status="detected")
    await _confirm(session, "old_field", status="dismissed")
    paths = await pii.confirmed_field_paths(session)
    assert "customer_name" in paths
    assert "guess_field" not in paths and "old_field" not in paths


# ── 11e: known values in free text ────────────────────────────────────

def test_values_at_collects_only_under_confirmed_keys():
    data = {"customer_name": "Ann Smith", "amount": 12345,
            "payer": {"iban": "DE001234"}, "ok": "no", "note": "Ann Smith called"}
    assert pii.values_at(data, PATHS) == ["Ann Smith", "DE001234"]


def test_values_at_skips_short_and_marker_values():
    data = {"customer_name": "Al", "iban": pii.REDACTED}
    assert pii.values_at(data, PATHS) == []
    assert pii.values_at({"customer_name": "Ann"}, set()) == []


def test_a_known_name_is_masked_in_prose_and_restored():
    p = Pseudonymizer()
    p.add_known(["Ann Smith"])
    out = p.redact("Letter to ann smith. Ann Smith's claim is open.")
    assert "ann smith" not in out.lower()
    assert p.restore(out) == "Letter to ann smith. Ann Smith's claim is open."


def test_known_values_respect_word_boundaries_and_length_order():
    p = Pseudonymizer()
    p.add_known(["Ann", "Ann Smith"])
    out = p.redact("Annual review for Ann Smith and Ann.")
    assert out.startswith("Annual review for ")
    assert "Smith" not in out and out.count("⟨FIELD_") == 2


def test_without_known_values_the_pseudonymizer_is_unchanged():
    text = "Mail ann@example.com about Ann Smith, ref 1234567890."
    assert Pseudonymizer().redact(text) == \
        "Mail ⟨EMAIL_1⟩ about Ann Smith, ref ⟨NUM_1⟩."


class _Case:
    data = {"customer_name": "Ann Smith"}


@pytest.mark.asyncio
async def test_masking_is_off_unless_switched_on(session):
    await _confirm(session, "customer_name")
    p = Pseudonymizer()
    assert await add_confirmed_fields(p, session, _Case()) == 0
    assert p.redact("Ann Smith") == "Ann Smith"


@pytest.mark.asyncio
async def test_masking_on_uses_confirmed_fields_only(session):
    await _confirm(session, "customer_name")
    p = Pseudonymizer()
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(pii_mask_ai_egress=True)):
        assert await add_confirmed_fields(p, session, _Case()) == 1
        assert await add_confirmed_fields(Pseudonymizer(), session, None) == 0
    assert "Ann Smith" not in p.redact("Claim by Ann Smith")


@pytest.mark.asyncio
async def test_masking_never_raises(session):
    class Broken:
        @property
        def data(self):
            raise RuntimeError("boom")

    with patch("velaris_shared.config.get_settings",
               return_value=_settings(pii_mask_ai_egress=True)):
        assert await add_confirmed_fields(Pseudonymizer(), session, Broken()) == 0


# ── 11f: anonymize reaches the case's text ────────────────────────────

def _old(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _seed_case(session, data=None):
    case = CaseInstanceModel(
        case_type_id=uuid.uuid4(), case_type_version=1, status="closed",
        priority="medium", data=data or {"submitter_email": "ann@example.com"},
        created_by="test", tenant_id=None, updated_at=_old(1000))
    session.add(case)
    await session.flush()
    session.add(CaseMessageModel(case_id=case.id, author="agent-1",
                                 author_name="Ann Smith", body="My IBAN is DE00 1234"))
    conv_id = uuid.uuid4()
    endpoint = ChannelEndpointModel(tenant_id="default", channel="sms", provider="test",
                                    address=f"+49{uuid.uuid4().int % 10**9}")
    session.add(endpoint)
    await session.flush()
    session.add(ChannelConversationModel(
        id=conv_id, tenant_id="default", endpoint_id=endpoint.id, case_id=case.id,
        peer_address="+491701234567", status="open", metadata_json={"profile": "Ann"}))
    await session.flush()
    session.add(ChannelMessageModel(
        tenant_id="default", conversation_id=conv_id, case_id=case.id,
        channel="sms", direction="inbound", peer_address="+491701234567",
        body="It is Ann, call me", media=[{"url": "x"}], raw_payload={"From": "+49170"}))
    convo = CopilotConversationModel(user_id="agent-1", case_id=case.id)
    session.add(convo)
    await session.flush()
    session.add(CopilotMessageModel(conversation_id=convo.id, role="user",
                                    content="What is Ann Smith's address?"))
    await session.flush()
    return case


async def _policy(session):
    row = RetentionPolicyModel(name=f"anon-{uuid.uuid4().hex[:6]}",
                               resource_type="resolved_cases", retention_days=30,
                               action=retention.ANONYMIZE, enabled=True)
    session.add(row)
    await session.flush()
    return row


async def _texts(session, case):
    from sqlalchemy import select
    msg = (await session.execute(select(CaseMessageModel).where(
        CaseMessageModel.case_id == case.id))).scalars().one()
    ch = (await session.execute(select(ChannelMessageModel).where(
        ChannelMessageModel.case_id == case.id))).scalars().one()
    ai = (await session.execute(
        select(CopilotMessageModel).join(
            CopilotConversationModel,
            CopilotConversationModel.id == CopilotMessageModel.conversation_id)
        .where(CopilotConversationModel.case_id == case.id))).scalars().one()
    for row in (msg, ch, ai):
        await session.refresh(row)
    return msg, ch, ai


@pytest.mark.asyncio
async def test_off_the_case_text_is_left_exactly_as_it_was(session):
    case = await _seed_case(session)
    out = await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()
    await session.refresh(case)
    msg, ch, ai = await _texts(session, case)

    assert out["rows"] == 1
    assert case.data["submitter_email"] == "[anonymized]"
    assert retention.ANON_TEXT_MARKER not in case.data
    assert (msg.body, msg.author_name) == ("My IBAN is DE00 1234", "Ann Smith")
    assert (ch.body, ch.peer_address) == ("It is Ann, call me", "+491701234567")
    assert ai.content == "What is Ann Smith's address?"


@pytest.mark.asyncio
async def test_on_the_text_is_blanked_and_the_rows_survive(session):
    case = await _seed_case(session)
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(retention_anonymize_case_text=True)):
        out = await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()
    await session.refresh(case)
    msg, ch, ai = await _texts(session, case)

    assert out["rows"] == 1
    assert case.data[retention.ANON_MARKER] and case.data[retention.ANON_TEXT_MARKER]
    assert (msg.body, msg.author_name, msg.author) == ("[anonymized]", None, "agent-1")
    assert (ch.body, ch.peer_address, ch.media, ch.raw_payload) == \
        ("[anonymized]", "[anonymized]", [], {})
    assert (ch.channel, ch.direction) == ("sms", "inbound")
    assert ai.content == "[anonymized]"


@pytest.mark.asyncio
async def test_on_a_case_anonymized_earlier_is_swept_once(session):
    case = await _seed_case(session)
    policy = await _policy(session)
    await retention.apply_policy(session, policy, apply=True)      # before the switch
    await session.flush()
    # Anonymizing a case stamps `updated_at`, so it is only selected again once
    # it has aged past the policy's window a second time. Age it.
    from sqlalchemy import update
    await session.execute(update(CaseInstanceModel).where(
        CaseInstanceModel.id == case.id).values(updated_at=_old(1000)))
    await session.flush()
    await session.refresh(case)

    cfg = _settings(retention_anonymize_case_text=True)
    with patch("velaris_shared.config.get_settings", return_value=cfg):
        dry = await retention.apply_policy(session, policy, apply=False)
        first = await retention.apply_policy(session, policy, apply=True)
        await session.flush()
        second = await retention.apply_policy(session, policy, apply=True)
    msg, ch, ai = await _texts(session, case)

    assert (dry["rows"], first["rows"], second["rows"]) == (1, 1, 0)
    assert msg.body == ch.body == ai.content == "[anonymized]"


@pytest.mark.asyncio
async def test_on_another_cases_text_is_not_touched(session):
    old = await _seed_case(session)
    live = await _seed_case(session)
    live.status = "open"
    live.updated_at = datetime.now(timezone.utc)
    await session.flush()
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(retention_anonymize_case_text=True)):
        await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()

    assert (await _texts(session, old))[0].body == "[anonymized]"
    msg, ch, ai = await _texts(session, live)
    assert (msg.body, ch.peer_address, ai.content) == \
        ("My IBAN is DE00 1234", "+491701234567", "What is Ann Smith's address?")


async def _thread(session, case):
    from sqlalchemy import select
    row = (await session.execute(select(ChannelConversationModel).where(
        ChannelConversationModel.case_id == case.id))).scalars().one()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_off_the_conversation_keeps_its_number_and_stays_open(session):
    case = await _seed_case(session)
    await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()
    thread = await _thread(session, case)
    assert (thread.peer_address, thread.status, thread.metadata_json) == \
        ("+491701234567", "open", {"profile": "Ann"})


@pytest.mark.asyncio
async def test_on_the_conversation_loses_the_number_whole_and_is_closed(session):
    case = await _seed_case(session)
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(retention_anonymize_case_text=True)):
        await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()
    thread = await _thread(session, case)
    assert (thread.peer_address, thread.status, thread.metadata_json) == \
        ("[anonymized]", "closed", {})
    assert "4567" not in thread.peer_address, "not even the last four digits"
    assert thread.case_id == case.id and thread.endpoint_id is not None


@pytest.mark.asyncio
async def test_on_a_new_message_from_that_number_does_not_find_the_old_thread(session):
    """Routing matches the full number on a conversation that is not closed."""
    from sqlalchemy import select
    case = await _seed_case(session)
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(retention_anonymize_case_text=True)):
        await retention.apply_policy(session, await _policy(session), apply=True)
    await session.flush()
    found = (await session.execute(select(ChannelConversationModel).where(
        ChannelConversationModel.peer_address == "+491701234567",
        ChannelConversationModel.status != "closed"))).scalars().all()
    assert found == []
