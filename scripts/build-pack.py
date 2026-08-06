#!/usr/bin/env python3
"""Build an Accelerator Pack directory into an installable `.hxapp`.

    python scripts/build-pack.py packs/velaris-claims-accelerator
    python scripts/build-pack.py packs/*                 # all of them
    python scripts/build-pack.py packs/... --out dist/

Emits `<pack>.hxapp` plus its SHA-256, which is what the marketplace index
(`sources.json`) pins — the checksum is verified at install before the bundle is
opened, so publishing the file without the digest makes it uninstallable.

The zip layout is exactly what `apps/packager.py` writes and what both the
conformance gate and `marketplace/packs.py` read: `manifest.json`, `pack.json`,
and one file per bundle section.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "case-service"))

# Kept in step with marketplace/packs.py — a section added there but not here
# would simply never ship, which is the kind of gap that shows up much later.
SECTIONS = (
    "data_models", "business_calendars", "access_roles", "access_groups",
    "form_definitions", "rule_definitions", "process_definitions",
    "case_types", "stages", "steps", "escalation_trees", "portals",
)


def _fail(msg: str) -> None:
    print(f"  \033[0;31m✗ {msg}\033[0m")
    sys.exit(1)


def validate_expressions(pack_dir: Path) -> None:
    """Refuse to build a pack the platform would reject at install.

    `marketplace/packs.py` checks this too — that is the enforcement point and
    it stays. Checking here as well means a bad expression is caught by whoever
    is building the pack, rather than by the customer trying to install it.
    """
    rules_file = pack_dir / "rule_definitions.json"
    if not rules_file.exists():
        return
    try:
        from case_service.core.safe_expression import Classification, classify_expression
    except ImportError:
        print("  ⚠ safe_expression unavailable — expression check skipped")
        return

    bad: list[str] = []
    for rule in json.loads(rules_file.read_text()):
        if rule.get("rule_type") not in ("expression", "declare_expression"):
            continue
        expr = (rule.get("definition_json") or {}).get("expression", "")
        classification, reason = classify_expression(expr)
        if classification is not Classification.CONFORMING:
            bad.append(f"{rule.get('name')}: {reason or classification.value}")
    if bad:
        _fail("expression rules rejected by HxSandbox — " + "; ".join(bad))


def build(pack_dir: Path, out_dir: Path) -> Path:
    meta_file = pack_dir / "pack.json"
    if not meta_file.exists():
        _fail(f"{pack_dir} has no pack.json")
    meta = json.loads(meta_file.read_text())

    for field in ("id", "name", "version", "vertical"):
        if not meta.get(field):
            _fail(f"pack.json is missing '{field}'")

    validate_expressions(pack_dir)

    # The marketplace manifest. `type` MUST be accelerator_pack or the install
    # path refuses it; `outbound_domains` is empty because a content pack has no
    # egress — it ships no code that could make a request.
    manifest = {
        "id": meta["id"],
        "name": meta["name"],
        "version": meta["version"],
        "type": "accelerator_pack",
        "publisher": meta.get("author", "Velaris"),
        "publisher_tier": "official",
        "outbound_domains": [],
        "manifest_version": 1,
        "runtime": "python",
        "description": meta.get("summary", ""),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pack_dir.name}.hxapp"

    sections_written = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("pack.json", json.dumps(meta, indent=2))
        for section in SECTIONS:
            src = pack_dir / f"{section}.json"
            if not src.exists():
                continue
            try:
                parsed = json.loads(src.read_text())
            except json.JSONDecodeError as exc:
                _fail(f"{src.name} is not valid JSON: {exc}")
            if not isinstance(parsed, list):
                _fail(f"{src.name} must be a JSON array")
            zf.writestr(f"{section}.json", json.dumps(parsed, indent=2))
            sections_written += 1

    if sections_written == 0:
        out_path.unlink(missing_ok=True)
        _fail(f"{pack_dir} contains no bundle sections — nothing to provision")

    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    (out_dir / f"{pack_dir.name}.hxapp.sha256").write_text(digest + "\n")

    size_kb = out_path.stat().st_size / 1024
    print(f"  \033[0;32m✓\033[0m {out_path.name}  ({sections_written} sections, {size_kb:.1f} KB)")
    print(f"    sha256: {digest}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Accelerator Pack .hxapp bundles")
    ap.add_argument("packs", nargs="+", help="pack directories")
    ap.add_argument("--out", default=str(REPO / "dist" / "packs"), help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.out)
    built = 0
    for p in args.packs:
        pack_dir = Path(p)
        if not pack_dir.is_dir():
            continue
        build(pack_dir, out_dir)
        built += 1

    if built == 0:
        _fail("no pack directories matched")
    print(f"\n  {built} pack(s) → {out_dir}")
    print("  Publish the .hxapp + its sha256 to the Marketplace repo's official/ folder.")


if __name__ == "__main__":
    main()
