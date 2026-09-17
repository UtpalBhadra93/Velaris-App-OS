"""HxNexus — document text extractor.

Supported formats:
  PDF  — via pypdf; text layers extracted. An image-only page has no text
         layer, and that is what `extract_text_async` falls back to OCR for.
  DOCX — via python-docx; paragraph text concatenated.
  TXT / HTML / XML — raw UTF-8 decode.
  Images — no text layer exists at all, so OCR is the only path.

**Two entry points, and the async one is the one to call.** `extract_text` reads
a text layer and nothing else, which is why a SCANNED document has always come
back empty from every caller. `extract_text_async` tries that first and falls
back to the OCR engine — which is a no-op stub unless an operator has switched
one on, so the behaviour of an install that changes nothing does not change.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def extract_text(data: bytes, content_type: str) -> str:
    """Extract plain text from document bytes. Returns empty string on failure."""
    ct = (content_type or "").lower()

    if "pdf" in ct:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages).strip()
        except Exception as exc:
            log.warning("HxNexus PDF extraction failed: %s", exc)
            return ""

    if "wordprocessingml" in ct or ct.endswith(".docx"):
        try:
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs if p.text.strip()).strip()
        except Exception as exc:
            log.warning("HxNexus DOCX extraction failed: %s", exc)
            return ""

    if "text" in ct or "html" in ct or "xml" in ct:
        try:
            return data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    return ""


async def extract_text_async(data: bytes, content_type: str) -> str:
    """The text layer if there is one, otherwise OCR.

    **Why this exists.** `documents/ocr.py` has had a registry, a protocol and a
    `get_ocr_engine()` since P24, and until 2026-09-09 **nothing called it** —
    the same shape as HxShield's detector, which had eight patterns and one
    caller. A real engine could have been registered and no document would have
    been read by it.

    The text layer is tried first and OCR is the fallback, never the default:
    reading a born-digital PDF's own text is exact, free and instant, while OCR
    is lossy and costs a second per page. A document with a text layer must not
    pay for one.
    """
    text = extract_text(data, content_type)
    if text and text.strip():
        return text

    ct = (content_type or "").lower()
    ocrable = "pdf" in ct or ct.startswith("image/")
    if not ocrable:
        return text

    try:
        from case_service.documents.ocr import get_ocr_engine

        ocr_text = await get_ocr_engine().extract_text(data, ct)
    except Exception as exc:  # noqa: BLE001
        # An enrichment that raises is an indexing run that fails.
        log.warning("OCR fallback failed: %s", exc)
        return text
    if ocr_text and ocr_text.strip():
        log.info("OCR recovered %d characters from a document with no text layer",
                 len(ocr_text))
        return ocr_text.strip()
    return text
