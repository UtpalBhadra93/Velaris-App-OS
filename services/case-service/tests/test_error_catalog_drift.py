"""The error catalog must not silently rot.

Three files describe the same error codes and had drifted before anyone
noticed: the registry knew a code the catalog never documented, and the Studio
catalog was missing two the backend could raise. Nothing detected it, because
each file is individually valid.

`KNOWN_CODES` already fails loudly when code *raises* an undocumented code. The
gap this closes is the other direction — a code added to the registry and then
never written up, which produces a deep link to a page explaining nothing.

**`catalog.ts` is deliberately allowed to be a superset.** It documents ~38
user-facing failures that are not coded backend errors at all (permission
hints, empty states, browser-side problems). Those are troubleshooting content,
not duplicates, and generating the file from the registry would delete them.
What it must not do is *miss* a code the backend can emit.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from case_service.core import error_codes

_REPO = Path(__file__).resolve().parents[3]
_MARKDOWN = _REPO / "docs" / "Future" / "error-catalog.md"
_STUDIO_CATALOG = _REPO / "studio" / "src" / "shared" / "errors" / "catalog.ts"


def _documented_in_markdown() -> set[str]:
    return set(re.findall(r"^### `(VEL-[A-Z0-9-]+)`", _MARKDOWN.read_text(), re.M))


def _documented_in_studio() -> set[str]:
    return set(re.findall(r'code:\s*"(VEL-[A-Z0-9-]+)"', _STUDIO_CATALOG.read_text()))


@pytest.mark.skipif(not _MARKDOWN.exists(), reason="docs not present in this checkout")
def test_every_known_code_is_documented_in_the_catalog():
    missing = error_codes.KNOWN_CODES - _documented_in_markdown()
    assert not missing, (
        f"{len(missing)} code(s) in KNOWN_CODES have no entry in error-catalog.md: "
        f"{sorted(missing)}. Each one deep-links a user to a page that explains "
        f"nothing — which is worse than returning no code at all."
    )


@pytest.mark.skipif(not _MARKDOWN.exists(), reason="docs not present in this checkout")
def test_catalog_documents_no_code_the_app_cannot_raise():
    """A documented code the app never emits is a promise it cannot keep."""
    orphans = _documented_in_markdown() - error_codes.KNOWN_CODES
    assert not orphans, (
        f"error-catalog.md documents {sorted(orphans)}, which are absent from "
        f"KNOWN_CODES. Either add the constant or mark the entry _(proposed)_."
    )


@pytest.mark.skipif(not _STUDIO_CATALOG.exists(), reason="studio not present")
def test_studio_catalog_covers_every_backend_code():
    """Studio may document more than the backend raises — never fewer.

    A code that reaches the browser with no Studio entry renders as bare text
    instead of a link, so the user is handed an identifier and no explanation.
    """
    missing = error_codes.KNOWN_CODES - _documented_in_studio()
    assert not missing, (
        f"{len(missing)} backend code(s) missing from studio catalog.ts: "
        f"{sorted(missing)}. ErrorCodeLink will not offer a link for these."
    )


def test_code_format_is_uniform():
    """`VEL-<SUBSYSTEM>-<SLUG>-<HTTP>`, so the prefix stays sortable."""
    bad = [c for c in error_codes.KNOWN_CODES
           if not re.fullmatch(r"VEL-[A-Z]+-[A-Z0-9-]+-\d{3}", c)]
    assert not bad, f"malformed codes: {sorted(bad)}"
