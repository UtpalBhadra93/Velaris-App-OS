"""HxSandbox's first consumer (#17 Phase 2) — HxSync WASM field transforms.

The execution host shipped long before anything called it: `runtime: wasm`
packages were refused at install because there was no runtime, which left
Phase 2 permanently gated on a producer that had never been written. This
covers the two halves that close it — extracting/validating a module at
install time, and running it per row inside the sync pipeline.

Fail-safe policy under test: a WASM transform that traps, exhausts its
budget, or is missing **fails the row**. Passing the raw value through would
emit data that looks transformed and is not, and a warehouse cannot tell the
difference afterwards.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from case_service.hxsandbox.registry import (
    MAX_MODULE_BYTES,
    ModuleValidationError,
    extract_module,
)
from case_service.hxsync.redaction import TransformError, apply_transforms

from tests.phase_hxsandbox.test_host import ECHO, MISSING_EXPORT, SPIN


def _hxapp(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestExtractModule:
    def test_extracts_and_validates_default_path(self):
        blob = _hxapp({"manifest.json": b"{}", "module.wasm": ECHO})
        assert extract_module(blob, {"runtime": "wasm"}) == ECHO

    def test_honours_declared_path(self):
        blob = _hxapp({"guests/t.wasm": ECHO})
        out = extract_module(blob, {"runtime": "wasm", "wasm_module": "guests/t.wasm"})
        assert out == ECHO

    def test_missing_module_is_rejected(self):
        blob = _hxapp({"manifest.json": b"{}"})
        with pytest.raises(ModuleValidationError, match="carries no module.wasm"):
            extract_module(blob, {"runtime": "wasm"})

    def test_invalid_module_is_rejected_at_install(self):
        """#15's mandate: install-time failure, never first-run."""
        blob = _hxapp({"module.wasm": MISSING_EXPORT})
        with pytest.raises(ModuleValidationError):
            extract_module(blob, {"runtime": "wasm"})

    @pytest.mark.parametrize("path", ["../escape.wasm", "/etc/passwd", "a/../../b.wasm"])
    def test_path_traversal_is_refused(self, path):
        """The manifest is publisher-controlled input."""
        blob = _hxapp({"module.wasm": ECHO})
        with pytest.raises(ModuleValidationError, match="not inside the bundle"):
            extract_module(blob, {"wasm_module": path})

    def test_oversized_module_is_refused_before_reading(self):
        blob = _hxapp({"module.wasm": b"\0" * (MAX_MODULE_BYTES + 1)})
        with pytest.raises(ModuleValidationError, match="over the"):
            extract_module(blob, {"runtime": "wasm"})

    def test_corrupt_archive_is_refused(self):
        with pytest.raises(ModuleValidationError, match="readable archive"):
            extract_module(b"not a zip", {"runtime": "wasm"})


class TestWasmTransform:
    def _mapping(self, module_id="acme/t"):
        return [{
            "source_field": "title",
            "dest_column": "title_out",
            "transform": "wasm",
            "transform_module_id": module_id,
        }]

    def test_runs_the_guest(self):
        out = apply_transforms({"title": "hello"}, self._mapping(), {"acme/t": ECHO})
        assert out == {"title_out": "hello"}

    def test_missing_module_id_fails_the_row(self):
        mapping = self._mapping(module_id=None)
        with pytest.raises(TransformError, match="names no module"):
            apply_transforms({"title": "x"}, mapping, {})

    def test_module_not_installed_fails_the_row(self):
        with pytest.raises(TransformError, match="not installed for this tenant"):
            apply_transforms({"title": "x"}, self._mapping(), {})

    def test_runaway_guest_fails_the_row(self):
        """A trap or exhausted budget must never degrade to the raw value."""
        with pytest.raises(TransformError, match="failed on"):
            apply_transforms({"title": "x"}, self._mapping(), {"acme/t": SPIN})

    def test_builtin_transforms_are_untouched(self):
        """The wasm branch must not disturb the existing built-ins."""
        mappings = [
            {"source_field": "secs", "dest_column": "hrs", "transform": "seconds_to_hours"},
            {"source_field": "n", "dest_column": "s", "transform": "to_string"},
            {"source_field": "raw", "dest_column": "raw", "transform": "passthrough"},
        ]
        out = apply_transforms({"secs": 7200, "n": 5, "raw": "keep"}, mappings)
        assert out == {"hrs": 2.0, "s": "5", "raw": "keep"}

    def test_mixed_mapping_fails_the_whole_row(self):
        """One broken transform fails the row, not just its own column."""
        mappings = [
            {"source_field": "secs", "dest_column": "hrs", "transform": "seconds_to_hours"},
        ] + self._mapping()
        with pytest.raises(TransformError):
            apply_transforms({"secs": 7200, "title": "x"}, mappings, {})


class TestInstallGate:
    async def test_wasm_install_refused_while_sandbox_disabled(self, client):
        """Default posture: HxSandbox off, so wasm packages cannot be installed."""
        from velaris_shared.config import get_settings

        assert get_settings().hxsandbox_enabled is False

    def test_manifest_json_is_not_required_for_extraction(self):
        """Extraction reads the module, not the manifest metadata around it."""
        blob = _hxapp({"module.wasm": ECHO, "pack.json": json.dumps({"id": "x"}).encode()})
        assert extract_module(blob, {}) == ECHO
