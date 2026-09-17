"""F-06 — where captured text comes from, and what had to be true to read it.

Three sources, and the list is closed on purpose: each one has its own consent
story, and a fourth is a code change precisely because somebody has to answer
that question for it before any text is read.

| Source | The text | What must already be true |
|---|---|---|
| `meeting` | The transcript HxMeet's own analysis produced | A **completed** intelligence run exists on a session belonging to this case |
| `email` | Subject + plain body of a message threaded to the case | The message is bound to this case |
| `voice_note` | Text the worker dictated | Nothing — it is the worker's own words, typed by voice |

**Capture never unseals a recording.** It reads the transcript HxMeet already
made, under HxMeet's consent gate, with HxMeet's tenant opt-in. Re-deriving that
consent here would mean two implementations of one rule, and the weaker one
would eventually be the one that ran.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseSessionIntelligenceModel,
    CaseSessionModel,
    EmailMessageModel,
)

log = logging.getLogger(__name__)

#: The closed set. Adding one is a release, never configuration.
SOURCE_KINDS = frozenset({"meeting", "email", "voice_note"})

#: A transcript can be long, and the context window is not the limit that
#: matters — a prompt that scrolls past the model's attention produces confident
#: nonsense about the middle of it. Truncation is REPORTED (see `truncated`), not
#: silent: "the model read the first hour of a two-hour meeting" is something a
#: reviewer must be able to know.
MAX_SOURCE_CHARS = 24_000


class SourceUnavailable(Exception):
    """The source cannot be read, with a machine-readable reason.

    Its own exception rather than a None return: every caller must decide what
    to record, and a reason that can be silently dropped will be.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(detail or reason)


@dataclass(frozen=True)
class Source:
    kind: str
    ref: str
    text: str
    locator: str | None
    truncated: bool = False

    @property
    def sha256(self) -> str:
        """The commitment: exactly the text the model will read.

        Hashed AFTER truncation, deliberately. A hash of the full transcript
        would claim the model saw material it never received.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _clip(text: str) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= MAX_SOURCE_CHARS:
        return text, False
    return text[:MAX_SOURCE_CHARS], True


async def resolve(
    session: AsyncSession, *, case_id: uuid.UUID, kind: str,
    ref: str | None = None, text: str | None = None,
) -> Source:
    """Turn a source reference into the text capture will read.

    Every path checks that the source belongs to **this case**. A capture that
    could read another case's email by id would be a cross-case read dressed up
    as a feature.
    """
    if kind not in SOURCE_KINDS:
        raise SourceUnavailable("unknown_source_kind", kind)

    if kind == "voice_note":
        body, cut = _clip(text or "")
        if not body:
            raise SourceUnavailable("empty_source", "no dictated text")
        # No stored artifact and no locator: the STT path is
        # transcribe-and-discard, and inventing a reference to audio nobody kept
        # would be a citation to nothing.
        return Source(kind, "adhoc", body, None, cut)

    if not ref:
        raise SourceUnavailable("missing_source_ref", f"{kind} needs a source id")

    if kind == "email":
        try:
            msg_id = uuid.UUID(str(ref))
        except (ValueError, AttributeError):
            raise SourceUnavailable("missing_source_ref", "email ref must be a uuid")
        msg = await session.get(EmailMessageModel, msg_id)
        if msg is None or msg.case_id != case_id:
            raise SourceUnavailable("not_on_this_case", "no such message on this case")
        body, cut = _clip(f"Subject: {msg.subject or ''}\n\n{msg.body_text or ''}")
        if not (msg.body_text or "").strip():
            raise SourceUnavailable("empty_source", "the message has no text body")
        return Source(kind, str(msg_id), body, msg.message_id, cut)

    # meeting
    try:
        session_id = uuid.UUID(str(ref))
    except (ValueError, AttributeError):
        raise SourceUnavailable("missing_source_ref", "meeting ref must be a uuid")
    meet = await session.get(CaseSessionModel, session_id)
    if meet is None or meet.case_id != case_id:
        raise SourceUnavailable("not_on_this_case", "no such session on this case")
    intel = await session.get(CaseSessionIntelligenceModel, session_id)
    if intel is None or intel.status != "complete" or not intel.transcript_document_id:
        # The refusal that carries the consent rule. HxMeet produces a
        # transcript only for a sealed recording under its own consent gate and
        # tenant opt-in; no transcript means that gate was never passed, and
        # capture has no business going looking for the audio itself.
        raise SourceUnavailable(
            "no_analysed_transcript",
            "this session has no completed HxMeet analysis to read")

    from case_service.documents.service import DocumentService

    try:
        data, _name, _ctype = await DocumentService().download(
            session, intel.transcript_document_id)
        body_text = data.decode("utf-8", errors="replace")
    except Exception as exc:                                   # noqa: BLE001
        raise SourceUnavailable("transcript_unreadable", str(exc)[:200])

    body, cut = _clip(body_text)
    if not body:
        raise SourceUnavailable("empty_source", "the transcript is empty")
    return Source(kind, str(session_id), body,
                  str(intel.transcript_document_id), cut)


async def summarise(session: AsyncSession, source: Source) -> dict[str, Any]:
    """What the digest shows about where a run's text came from."""
    return {
        "kind": source.kind,
        "ref": source.ref,
        "locator": source.locator,
        "chars": len(source.text),
        "truncated": source.truncated,
    }
