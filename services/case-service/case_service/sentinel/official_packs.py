"""HxSentinel P4b — the bundled OFFICIAL signal-pack catalogue.

A curated set of vertical rule bundles shipped in the image as content (like the
taxonomy / official registry), so a tenant can browse and one-click install them
without hand-writing a manifest. Every pack here is validated CONFORMING at
import — a malformed or unsafe bundled pack fails startup loudly rather than
shipping broken.

These are ordinary manifests: the same format an external/community pack uses,
run through the same install + safe-expression validation. "Official" only means
"bundled + curated", it grants no extra capability.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.sentinel import packs as _packs

#: Curated official packs. Expressions reference only the documented flat
#: feature context (see packs.FEATURE_KEYS) and are strict-grammar (CONFORMING).
OFFICIAL_PACKS: tuple[dict, ...] = (
    {
        "id": "generic-fraud",
        "name": "Generic Fraud Signals",
        "vertical": "general",
        "version": "1.0.0",
        "description": "Cross-industry fraud heuristics over shared identities, documents, and resemblance.",
        "taxonomy": [],
        "rules": [
            {"key": "identity_velocity_high",
             "when": "phone_velocity >= 4 or email_velocity >= 4 or address_velocity >= 4",
             "severity": "medium",
             "title": "Identity reused across many cases",
             "message": "An identity on this case recurs across 4+ cases — a common synthetic-identity signal. Verify."},
            {"key": "document_reused_from_bad",
             "when": "shares_doc_with_bad",
             "severity": "high",
             "title": "Document reused from a confirmed-bad case",
             "message": "A document here also appears on a confirmed fraud/criminal case. Re-verify it."},
            {"key": "resembles_bad_cases",
             "when": "shared_signals_with_bad >= 2",
             "severity": "medium",
             "title": "Resembles confirmed-bad cases",
             "message": "This case shares multiple signals with a confirmed-bad case. Review."},
        ],
    },
    {
        "id": "telco-fraud",
        "name": "Telecom Fraud Signals",
        "vertical": "telco",
        "version": "1.0.0",
        "description": "SIM-swap and subscription-fraud heuristics for telecom onboarding.",
        "taxonomy": [
            {"key": "sim_swap", "display": "SIM swap", "is_negative": True,
             "description": "Account taken over via a fraudulent SIM swap."},
        ],
        "rules": [
            {"key": "sim_swap_velocity",
             "when": "phone_velocity >= 3 and amount > 1000",
             "severity": "high",
             "title": "High-value activity on a heavily-reused number",
             "message": "This phone recurs across 3+ cases with a >1000 amount — a SIM-swap pattern. Enhanced verification."},
            {"key": "high_value_thin_identity",
             "when": "amount > 5000 and phone_velocity >= 2",
             "severity": "medium",
             "title": "High-value transaction on a reused identity",
             "message": "A >5000 amount on an identity seen on other cases. Confirm the subscriber."},
        ],
    },
    {
        "id": "lending",
        "name": "Lending & Credit Signals",
        "vertical": "lending",
        "version": "1.0.0",
        "description": "Application-fraud heuristics for lending: amount outliers and shared-identity defaults.",
        "taxonomy": [],
        "rules": [
            {"key": "amount_outlier",
             "when": "amount_zscore > 3",
             "severity": "high",
             "title": "Loan amount is a statistical outlier",
             "message": "The requested amount is 3+ std from this product's norm. Review affordability and intent."},
            {"key": "shared_identity_high_value",
             "when": "shared_signals_with_bad >= 1 and amount > 10000",
             "severity": "high",
             "title": "High-value application linked to a confirmed-bad case",
             "message": "A >10000 application sharing a signal with a confirmed-bad case. Manual underwriting."},
        ],
    },
)

_BY_ID = {p["id"]: p for p in OFFICIAL_PACKS}

# Fail loudly at import if a bundled pack is malformed or unsafe.
for _p in OFFICIAL_PACKS:
    _packs.validate_manifest(_p)


def catalogue() -> list[dict]:
    """Catalogue entries (no rule bodies — a summary for the browser)."""
    return [
        {"pack_id": p["id"], "name": p["name"], "vertical": p.get("vertical"),
         "version": p["version"], "description": p.get("description", ""),
         "rule_count": len(p["rules"])}
        for p in OFFICIAL_PACKS
    ]


def get_official(pack_id: str) -> dict | None:
    return _BY_ID.get(pack_id)
