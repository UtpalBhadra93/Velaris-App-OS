"""HxSync — PII redaction engine + field transforms.

Applies redaction rules to a row before it leaves Velaris.
Actions: hash (SHA-256 hex), drop (remove key), mask (replace with ***).

Transforms rename a source field to its destination column and optionally
reshape the value. Besides the built-ins, a mapping may name a customer-
authored WASM module (`transform="wasm"`) executed inside HxSandbox (#17).
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)


class TransformError(Exception):
    """A field transform could not be applied.

    Raised only by the WASM path. The pipeline treats it as a row failure:
    a customer transform that traps, exhausts its budget, or is missing must
    not silently emit an untransformed value that downstream consumers would
    read as though it had been transformed.
    """


def apply_redaction(row: dict, rules: list[dict]) -> dict:
    """Return a copy of row with redaction rules applied."""
    result = dict(row)
    for rule in rules:
        field = rule["field_path"]
        action = rule.get("action", "hash")
        if field not in result:
            continue
        val = result[field]
        if action == "drop":
            del result[field]
        elif action == "mask":
            result[field] = "***"
        else:  # hash
            result[field] = hashlib.sha256(str(val).encode()).hexdigest() if val is not None else None
    return result


def apply_transforms(
    row: dict, mappings: list[dict], modules: dict[str, bytes] | None = None,
) -> dict:
    """Rename fields and apply transforms, returning a new dict keyed by dest_column.

    `modules` maps a package id to its validated WASM bytes, loaded once per
    sync run by the caller — a per-row database read would be pointless and a
    per-row publisher fetch unthinkable.

    Raises :class:`TransformError` if a `wasm` mapping cannot be executed.
    Built-in transforms keep their existing behaviour of leaving the value
    untouched when it cannot be converted.
    """
    result = {}
    for mapping in mappings:
        src = mapping["source_field"]
        dst = mapping["dest_column"]
        transform = mapping.get("transform", "passthrough")
        val = row.get(src)
        if transform == "wasm":
            result[dst] = _run_wasm_transform(mapping, val, modules or {})
            continue
        if transform == "seconds_to_hours" and val is not None:
            try:
                val = round(float(val) / 3600, 4)
            except (TypeError, ValueError):
                pass
        elif transform == "to_string" and val is not None:
            val = str(val)
        elif transform == "to_int" and val is not None:
            try:
                val = int(val)
            except (TypeError, ValueError):
                pass
        result[dst] = val
    return result


def _run_wasm_transform(mapping: dict, value, modules: dict[str, bytes]):
    """Execute one customer-authored transform inside HxSandbox.

    Every failure mode raises rather than degrading: a missing module id, a
    module that was never installed for this tenant, a trap, or a resource
    limit. The alternative — passing the raw value through — would ship data
    that looks transformed and is not, and a warehouse cannot tell the
    difference after the fact.
    """
    module_id = mapping.get("transform_module_id")
    dst = mapping.get("dest_column")
    if not module_id:
        raise TransformError(
            f"mapping for {dst!r} declares transform 'wasm' but names no module")

    wasm_bytes = modules.get(module_id)
    if wasm_bytes is None:
        raise TransformError(
            f"WASM module {module_id!r} for {dst!r} is not installed for this tenant")

    from case_service.hxsandbox import HxSandboxError, run_transform

    try:
        return run_transform(wasm_bytes, value)
    except HxSandboxError as exc:
        raise TransformError(f"WASM transform {module_id!r} failed on {dst!r}: {exc}") from exc
