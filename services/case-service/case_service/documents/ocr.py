"""OCR — pluggable interface, with a real self-hosted engine behind a switch.

**What was here.** `StubOCREngine` returned `""` for every document, and it was
the only engine. HxNexus's `text_extractor.py` reads a PDF's *text layer*, so a
born-digital PDF worked and a **scanned** page — the actual OCR case, and the
one every insurer and council has boxes of — yielded nothing at all. Nothing
said so: the caller received an empty string, which is indistinguishable from a
blank page.

**What this adds, and what it deliberately does not.** `TesseractOCREngine`
shells out to the `tesseract` binary. That choice is the sovereignty one: OCR is
the classic thing to send to a cloud API, and a platform whose whole claim is
"your data stays on hardware you control" cannot answer scanned documents by
posting them to somebody else's endpoint. A commercial IDP — Azure Document
Intelligence, Textract, Google Document AI — is a **procurement decision with a
data-egress consequence**, and remains the operator's to make; the engine
registry below is the seam one arrives through.

**Three properties.**

* **Off unless the operator switches it on AND the binary is installed.**
  Unset, this module behaves exactly as it did: the stub, an empty string, no
  new dependency, no new package in the image.
* **No Python dependency.** It runs the binary through `subprocess`, so nothing
  is added to `pyproject.toml` and an install that does not want OCR carries no
  extra code. `pytesseract` would have been one import and one more thing to
  keep current.
* **Bounded.** A page cap, a byte cap and a timeout, because OCR on an
  adversarial PDF is a CPU-exhaustion primitive: a 10,000-page document would
  otherwise hold a worker for an hour.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

#: What tesseract is given directly. Anything else must be rasterised first.
IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp",
    "image/webp", "image/gif",
})

#: Pages rasterised from a PDF before OCR stops. A scanned form is a handful of
#: pages; a document that is hundreds is a report with a text layer, which
#: `text_extractor.py` already reads without any of this cost.
MAX_PDF_PAGES = 20

#: Bytes accepted at all. Above this the answer is "not OCRed", not "the worker
#: is busy for ten minutes".
MAX_INPUT_BYTES = 25 * 1024 * 1024

#: Seconds one document may take, rasterising included.
#:
#: **Indexing is synchronous BY DEFAULT.** `hxnexus/service.py::index_document`
#: is awaited inside the request, and `POST /hxnexus/cases/{id}/documents/index`
#: loops over every document in the case — so this number is multiplied by the
#: number of scanned documents in one request, not paid once. Halved from 60 for
#: that reason.
#:
#: Since migration 203 a caller may pass `background: true` instead, which
#: queues one job per document for the `document_index` scheduler action to
#: drain, so the timeout is paid by a schedule rather than by a request. The
#: synchronous path is unchanged and is still what an install gets if it asks
#: for nothing: see `hxnexus/index_queue.py`.
OCR_TIMEOUT_SECONDS = 30


class OCREngine(Protocol):
    async def extract_text(self, data: bytes, content_type: str) -> str:
        """Return extracted text (empty if unsupported)."""
        ...


class StubOCREngine:
    """No-op OCR. Returns empty string; the shipped default.

    It is deliberately still the default after a real engine exists: turning OCR
    on changes what a document *contains* as far as search, HxPrivacy's scan and
    every AI path are concerned, and that is an operator's decision, not an
    update's.
    """

    async def extract_text(self, data: bytes, content_type: str) -> str:
        return ""


class TesseractOCREngine:
    """Local OCR through the `tesseract` binary — nothing leaves the machine.

    `available()` is checked at registration rather than per call, but a missing
    binary at run time still returns "" rather than raising: OCR is an
    enrichment, and a document upload must not fail because an operator removed
    a package.
    """

    def __init__(self, *, language: str = "eng", timeout: int = OCR_TIMEOUT_SECONDS) -> None:
        self.language = language
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return shutil.which("tesseract") is not None

    @staticmethod
    def can_rasterise() -> bool:
        """PDFs need poppler's `pdftoppm`. Without it, images still work."""
        return shutil.which("pdftoppm") is not None

    async def _run(self, argv: list[str], cwd: str) -> tuple[int, bytes]:
        """Run a binary with no shell, bounded by the timeout.

        `create_subprocess_exec`, never `shell=True`: a filename reaches this
        from an upload, and a shell would make the document's own name part of
        the command.
        """
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            logger.debug("ocr: %s exited %s: %s", argv[0], process.returncode,
                         (stderr or b"")[:200])
        return process.returncode or 0, stdout or b""

    async def extract_text(self, data: bytes, content_type: str) -> str:
        content_type = (content_type or "").split(";")[0].strip().lower()
        if not data or len(data) > MAX_INPUT_BYTES:
            return ""
        if not self.available():
            logger.warning("ocr: tesseract is configured but not installed")
            return ""

        try:
            with tempfile.TemporaryDirectory(prefix="velaris-ocr-") as workdir:
                if content_type in IMAGE_TYPES:
                    pages = [self._write(workdir, "page.img", data)]
                elif content_type == "application/pdf":
                    pages = await self._rasterise(workdir, data)
                else:
                    return ""

                chunks = []
                for page in pages:
                    _, stdout = await self._run(
                        ["tesseract", str(page), "stdout", "-l", self.language],
                        cwd=workdir,
                    )
                    chunks.append(stdout.decode("utf-8", errors="replace"))
                return "\n".join(c.strip() for c in chunks if c.strip())
        except asyncio.TimeoutError:
            logger.warning("ocr: timed out after %ss — document not OCRed", self.timeout)
            return ""
        except Exception:  # noqa: BLE001
            # An enrichment that raises is an upload that fails. It is logged
            # loudly and the document is stored either way.
            logger.exception("ocr: extraction failed — document stored without text")
            return ""

    @staticmethod
    def _write(workdir: str, name: str, data: bytes) -> Path:
        path = Path(workdir) / name
        path.write_bytes(data)
        return path

    async def _rasterise(self, workdir: str, data: bytes) -> list[Path]:
        if not self.can_rasterise():
            logger.warning("ocr: pdftoppm (poppler) is not installed — PDFs are "
                           "not rasterised, so a scanned PDF yields nothing")
            return []
        source = self._write(workdir, "in.pdf", data)
        await self._run(
            ["pdftoppm", "-png", "-r", "200", "-f", "1", "-l", str(MAX_PDF_PAGES),
             str(source), "page"],
            cwd=workdir,
        )
        return sorted(Path(workdir).glob("page*.png"))


_engine: OCREngine | None = None


def get_ocr_engine() -> OCREngine:
    """The registered engine, choosing one on first use.

    The choice is made here rather than at import so a setting written after
    import — which is how the tests and the CLI both work — is still honoured.
    """
    global _engine
    if _engine is None:
        _engine = _engine_from_settings()
    return _engine


def _engine_from_settings() -> OCREngine:
    try:
        from velaris_shared.config import get_settings

        settings = get_settings()
        configured = (getattr(settings, "ocr_engine", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        configured = ""

    if configured in ("", "none", "stub"):
        return StubOCREngine()
    if configured == "tesseract":
        if not TesseractOCREngine.available():
            # Named, not silent. An operator who asked for OCR and got an empty
            # string every time would conclude the feature does not work.
            logger.error(
                "ocr: VELARIS_CASE_OCR_ENGINE=tesseract but the `tesseract` "
                "binary is not on PATH — install tesseract-ocr, or unset the "
                "variable. Falling back to no OCR.")
            return StubOCREngine()
        language = getattr(get_settings(), "ocr_language", "eng") or "eng"
        return TesseractOCREngine(language=language)

    logger.error("ocr: unknown engine %r — falling back to no OCR.", configured)
    return StubOCREngine()


def set_ocr_engine(engine: OCREngine) -> None:
    """Register a real engine (used by a plugin, or by a test)."""
    global _engine
    _engine = engine


def reset_ocr_engine() -> None:
    """Forget the cached choice, so the setting is read again."""
    global _engine
    _engine = None
