"""OCR — the stub stays the default, and the real engine is bounded.

`StubOCREngine` returned "" for every document and was the only engine, so a
SCANNED page yielded nothing and nothing said so — an empty string is
indistinguishable from a blank page. These tests pin the two halves that matter:
an install that sets nothing still gets exactly that behaviour, and the engine
that replaces it cannot be turned into a CPU-exhaustion primitive by an
adversarial upload.

**Not proven here:** a real `tesseract` run. The binary is not installed on this
machine and installing it needs root, so the subprocess boundary is exercised
with a stand-in. That gap is named rather than papered over.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio

import pytest

from case_service.documents import ocr


@pytest.fixture(autouse=True)
def _clean_engine():
    ocr.reset_ocr_engine()
    yield
    ocr.reset_ocr_engine()


class TestTheDefault:
    def test_an_install_that_sets_nothing_gets_no_ocr(self):
        """Switching OCR on changes what a document CONTAINS as far as search,
        HxPrivacy's scan and every AI path are concerned. That is an operator's
        decision, not an update's."""
        assert isinstance(ocr.get_ocr_engine(), ocr.StubOCREngine)

    async def test_the_stub_returns_empty_for_anything(self):
        assert await ocr.StubOCREngine().extract_text(b"anything", "image/png") == ""

    def test_asking_for_tesseract_without_the_binary_falls_back_loudly(
            self, monkeypatch, caplog):
        """An operator who asked for OCR and silently got an empty string every
        time would conclude the feature does not work."""
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: False))
        monkeypatch.setattr(ocr, "_engine_from_settings",
                            ocr._engine_from_settings)  # keep the real chooser
        from velaris_shared.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "ocr_engine", "tesseract", raising=False)
        with caplog.at_level("ERROR"):
            engine = ocr.get_ocr_engine()
        assert isinstance(engine, ocr.StubOCREngine)
        assert any("tesseract" in r.message.lower() for r in caplog.records)

    def test_an_unknown_engine_name_does_not_break_uploads(self, monkeypatch):
        from velaris_shared.config import get_settings
        monkeypatch.setattr(get_settings(), "ocr_engine", "magic-cloud-ocr", raising=False)
        assert isinstance(ocr.get_ocr_engine(), ocr.StubOCREngine)


class TestBounds:
    async def test_an_oversized_document_is_not_ocred(self, monkeypatch):
        """OCR on an adversarial document is a CPU-exhaustion primitive."""
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))
        engine = ocr.TesseractOCREngine()
        huge = b"x" * (ocr.MAX_INPUT_BYTES + 1)
        assert await engine.extract_text(huge, "image/png") == ""

    async def test_an_unsupported_type_is_skipped_rather_than_guessed(self, monkeypatch):
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))
        engine = ocr.TesseractOCREngine()
        assert await engine.extract_text(b"MZ...", "application/x-msdownload") == ""

    async def test_a_timeout_returns_nothing_rather_than_failing_the_upload(
            self, monkeypatch):
        """An enrichment that raises is an upload that fails."""
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))

        async def hang(self, argv, cwd):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(ocr.TesseractOCREngine, "_run", hang)
        engine = ocr.TesseractOCREngine()
        assert await engine.extract_text(b"\x89PNG...", "image/png") == ""

    async def test_a_crashing_binary_returns_nothing_rather_than_raising(
            self, monkeypatch):
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))

        async def boom(self, argv, cwd):
            raise OSError("no such file")

        monkeypatch.setattr(ocr.TesseractOCREngine, "_run", boom)
        assert await ocr.TesseractOCREngine().extract_text(b"x", "image/png") == ""

    def test_the_pdf_page_cap_is_small(self):
        """A scanned form is a handful of pages; a document that is hundreds has
        a text layer, which text_extractor.py already reads for free."""
        assert ocr.MAX_PDF_PAGES <= 50


class TestTheSubprocessBoundary:
    async def test_an_image_is_passed_to_the_binary_and_its_output_returned(
            self, monkeypatch):
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))
        seen: list[list[str]] = []

        async def fake_run(self, argv, cwd):
            seen.append(argv)
            return 0, b"POLICY NUMBER 12345\n"

        monkeypatch.setattr(ocr.TesseractOCREngine, "_run", fake_run)
        text = await ocr.TesseractOCREngine().extract_text(b"\x89PNG", "image/png")
        assert text == "POLICY NUMBER 12345"
        assert seen[0][0] == "tesseract"
        assert "-l" in seen[0] and "eng" in seen[0]

    async def test_a_pdf_is_rasterised_first_and_every_page_read(self, monkeypatch):
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))
        monkeypatch.setattr(ocr.TesseractOCREngine, "can_rasterise", staticmethod(lambda: True))
        calls: list[str] = []

        async def fake_run(self, argv, cwd):
            calls.append(argv[0])
            if argv[0] == "pdftoppm":
                from pathlib import Path
                for i in (1, 2):
                    (Path(cwd) / f"page-{i}.png").write_bytes(b"x")
                return 0, b""
            return 0, b"page text"

        monkeypatch.setattr(ocr.TesseractOCREngine, "_run", fake_run)
        text = await ocr.TesseractOCREngine().extract_text(b"%PDF-1.4", "application/pdf")
        assert calls[0] == "pdftoppm"
        assert calls.count("tesseract") == 2
        assert text.count("page text") == 2

    async def test_a_pdf_without_poppler_says_so_rather_than_pretending(
            self, monkeypatch, caplog):
        monkeypatch.setattr(ocr.TesseractOCREngine, "available", staticmethod(lambda: True))
        monkeypatch.setattr(ocr.TesseractOCREngine, "can_rasterise", staticmethod(lambda: False))
        with caplog.at_level("WARNING"):
            assert await ocr.TesseractOCREngine().extract_text(
                b"%PDF-1.4", "application/pdf") == ""
        assert any("pdftoppm" in r.message for r in caplog.records)

    def test_the_binary_is_never_run_through_a_shell(self):
        """A filename reaches this from an upload. A shell would make the
        document's own name part of the command."""
        import ast
        import inspect
        # Read the CODE, not the comment that explains the choice: the
        # docstring below says the word "shell" on purpose.
        tree = ast.parse(inspect.getsource(ocr.TesseractOCREngine._run).strip())
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "create_subprocess_exec" in calls
        assert "create_subprocess_shell" not in calls
        keywords = {
            kw.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
        }
        assert "shell" not in keywords


class TestItIsActuallyCalled:
    """`documents/ocr.py` had a registry, a protocol and `get_ocr_engine()`
    since P24 — and **nothing called it**. A real engine could have been
    registered and no document would ever have been read by one. The same shape
    as HxShield's detector: eight patterns, one caller, zero incidents."""

    async def test_a_text_layer_is_read_without_paying_for_ocr(self, monkeypatch):
        """Reading a born-digital PDF's own text is exact, free and instant.
        OCR is lossy and costs a second a page — a document with a text layer
        must not pay for one."""
        from case_service.hxnexus import text_extractor

        called = []

        class Loud:
            async def extract_text(self, data, content_type):
                called.append(content_type)
                return "OCR RAN"

        ocr.set_ocr_engine(Loud())
        text = await text_extractor.extract_text_async(b"plain words", "text/plain")
        assert text == "plain words"
        assert called == [], "OCR must not run when there is a text layer"

    async def test_an_image_reaches_the_engine(self, monkeypatch):
        """No text layer exists at all for an image, so this is the only path —
        and before the wiring it returned an empty string every time."""
        from case_service.hxnexus import text_extractor

        class Reader:
            async def extract_text(self, data, content_type):
                return "POLICY NUMBER 12345"

        ocr.set_ocr_engine(Reader())
        assert await text_extractor.extract_text_async(
            b"\x89PNG", "image/png") == "POLICY NUMBER 12345"

    async def test_a_failing_engine_does_not_fail_the_indexing_run(self, monkeypatch):
        from case_service.hxnexus import text_extractor

        class Broken:
            async def extract_text(self, data, content_type):
                raise RuntimeError("tesseract died")

        ocr.set_ocr_engine(Broken())
        assert await text_extractor.extract_text_async(b"\x89PNG", "image/png") == ""

    async def test_with_the_default_stub_nothing_changes(self):
        """The property that makes this safe to ship: an install that switches
        nothing on behaves exactly as it did."""
        from case_service.hxnexus import text_extractor

        ocr.reset_ocr_engine()
        assert await text_extractor.extract_text_async(b"\x89PNG", "image/png") == ""
