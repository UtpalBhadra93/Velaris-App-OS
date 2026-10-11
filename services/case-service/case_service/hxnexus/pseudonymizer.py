"""Group H (AI Egress Guard layer 6) — PII pseudonymization before egress.

Structured PII in outgoing text is replaced with stable placeholders before
it leaves the platform for an external AI provider; the mapping stays in
memory for the single call and real values are re-substituted into the
answer. The provider never sees the originals.

Covered (regex-detectable, structured PII):
  emails → ⟨EMAIL_n⟩ · phone numbers → ⟨PHONE_n⟩ · UUIDs → ⟨ID_n⟩
  long digit runs (8+, account/reference numbers) → ⟨NUM_n⟩

Honest limitation: free-text *names* are not reliably detectable without an
NER model and are NOT redacted. Documented in hxnexus.md.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ID",    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    # NUM before PHONE so contiguous account/reference numbers get the right label
    ("NUM",   re.compile(r"\b\d{8,}\b")),
    # International-ish phone: optional +, 8-15 digits with common separators
    ("PHONE", re.compile(r"(?<![\w.])\+?\d[\d ().\-]{6,16}\d(?![\w.])")),
]


class Pseudonymizer:
    """Stateful per-call redactor: redact() outgoing text, restore() the answer."""

    def __init__(self) -> None:
        self._forward: dict[str, str] = {}   # original -> placeholder
        self._reverse: dict[str, str] = {}   # placeholder -> original
        self._counters: dict[str, int] = {}

    @property
    def replaced_count(self) -> int:
        return len(self._forward)

    def _placeholder(self, kind: str, original: str) -> str:
        if original in self._forward:
            return self._forward[original]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        ph = f"⟨{kind}_{self._counters[kind]}⟩"
        self._forward[original] = ph
        self._reverse[ph] = original
        return ph

    def add_known(self, values) -> None:
        """Register literal values to mask wherever they appear (HxPrivacy 11e).

        The patterns below cannot see a name. A reviewer who confirmed that
        `customer_name` holds personal data has told us what the name *is* for
        this case — so that exact value is masked in the question, the context
        and anything else `redact` is handed, and restored in the answer.
        Longest first, so "Ann Smith" is taken before "Ann".
        """
        known = {str(v).strip() for v in (values or []) if str(v).strip()}
        self._known = sorted(known | set(getattr(self, "_known", [])),
                             key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in getattr(self, "_known", []):
            pattern = re.compile(r"(?<!\w)" + re.escape(value) + r"(?!\w)", re.IGNORECASE)
            text = pattern.sub(lambda m: self._placeholder("FIELD", m.group(0)), text)
        for kind, pattern in _PATTERNS:
            def _sub(m: re.Match, _kind: str = kind) -> str:
                value = m.group(0)
                # PHONE pattern can over-match digit groups already handled
                if value in self._forward:
                    return self._forward[value]
                return self._placeholder(_kind, value)
            text = pattern.sub(_sub, text)
        return text

    def restore(self, text: str) -> str:
        if not text:
            return text
        for placeholder, original in self._reverse.items():
            text = text.replace(placeholder, original)
        return text


async def add_confirmed_fields(pseudo: Pseudonymizer, session, case) -> int:
    """Mask the values of this case's confirmed personal-data fields (11e).

    Off unless `pii_mask_ai_egress` is set, and a no-op without a case. Every
    tenant's confirmed field names are used rather than the case's own: the
    finding's tenant is stored in more than one spelling, and masking a value
    that is then restored in the answer costs nothing, where missing one sends
    a name to a provider. Never raises — a failure here must not turn a
    question into a 500, and the pattern masking still applies.
    """
    try:
        from velaris_shared.config import get_settings
        if not get_settings().pii_mask_ai_egress or case is None:
            return 0
        from velaris_shared.db import pii_redaction as _pii

        paths = await _pii.confirmed_field_paths(session)
        values = _pii.values_at(getattr(case, "data", None) or {}, paths)
        pseudo.add_known(values)
        return len(values)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("confirmed-field masking skipped: %s", exc)
        return 0
