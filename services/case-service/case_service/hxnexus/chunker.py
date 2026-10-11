"""HxNexus — text chunker.

Two strategies:

* ``window`` (the default, and what every install has always run) — fixed
  overlapping character windows, blind to the text's own structure.
* ``structure`` — split on headings, then paragraphs, then sentences, and fall
  back to the window only for a single unit longer than a chunk. The section
  heading travels with every chunk cut from its section, so "what is the notice
  period?" can land on a paragraph that never repeats the word "termination".

Both write the same thing: a list of strings. A chunk carries no marker of the
strategy that cut it, so rows indexed by either are read and answered alike and
changing the setting re-indexes nothing.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

WINDOW = "window"
STRUCTURE = "structure"

_MAX_HEADING = 80
_MD_HEADING = re.compile(r"^#{1,6}\s+(\S.*)$")
_NUM_HEADING = re.compile(r"^\d+(\.\d+)*[.)]?\s+[A-Za-z]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def resolve_strategy(value: str | None) -> str:
    """Unset, empty or unrecognised all mean the window — never an error."""
    name = (value or "").strip().lower()
    if name in ("", WINDOW):
        return WINDOW
    if name == STRUCTURE:
        return STRUCTURE
    log.warning("HxNexus: unknown chunk strategy %r — using %r", value, WINDOW)
    return WINDOW


def chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 64,
    strategy: str | None = None,
) -> list[str]:
    """Split text into chunks of ~chunk_size characters."""
    if resolve_strategy(strategy) == STRUCTURE:
        return _chunk_structure(text, chunk_size, overlap)
    return _chunk_window(text, chunk_size, overlap)


def _chunk_window(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


# ── structure strategy ────────────────────────────────────────────────

def _heading_of(line: str) -> str | None:
    """The heading text if `line` reads as one, else None.

    Deliberately narrow: a false heading splits a paragraph in two and labels
    the second half with a sentence fragment, which is worse than missing one.
    """
    line = line.strip()
    if not line or len(line) > _MAX_HEADING:
        return None
    md = _MD_HEADING.match(line)
    if md:
        return md.group(1).strip(" #") or None
    if line[-1] in ".,;:!?":
        return None
    if _NUM_HEADING.match(line):
        return line
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        return line
    return None


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """[(heading, [paragraph, …])] in document order; the heading may be ''."""
    sections: list[tuple[str, list[str]]] = []
    heading, pending, paragraphs, para = "", [], [], []

    def close_para():
        if para:
            paragraphs.append(" ".join(para))
            para.clear()

    def close_section():
        close_para()
        if paragraphs:
            sections.append((heading, list(paragraphs)))
            paragraphs.clear()

    prev_blank = True
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            close_para()
            prev_blank = True
            continue
        found = _heading_of(line) if prev_blank else None
        if found:
            close_section()
            # Headings with nothing between them are a parent and its child.
            pending.append(found)
            heading = " > ".join(pending[-3:])[: _MAX_HEADING * 2]
        else:
            pending.clear()
            para.append(line)
        # A heading keeps the "after a break" state, so one straight under
        # another is still seen as a heading.
        prev_blank = bool(found)
    close_section()
    if pending and not sections:
        # Nothing but headings: the text is still worth indexing.
        sections.append(("", pending))
    return sections


def _pack(units: list[str], budget: int, joiner: str) -> list[str]:
    """Greedily join whole units into pieces no longer than `budget`."""
    out, cur = [], ""
    for unit in units:
        if cur and len(cur) + len(joiner) + len(unit) > budget:
            out.append(cur)
            cur = unit
        else:
            cur = f"{cur}{joiner}{unit}" if cur else unit
    if cur:
        out.append(cur)
    return out


def _split_paragraph(paragraph: str, budget: int, overlap: int) -> list[str]:
    """One over-long paragraph → sentence-packed pieces, windowed as a last resort."""
    units: list[str] = []
    for sentence in (s.strip() for s in _SENTENCE_END.split(paragraph)):
        if not sentence:
            continue
        if len(sentence) <= budget:
            units.append(sentence)
        else:
            units.extend(_chunk_window(sentence, budget, min(overlap, budget // 2)))
    return _pack(units, budget, " ")


def _chunk_structure(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    for heading, paragraphs in _sections(text or ""):
        prefix = f"{heading}\n" if heading else ""
        # A long heading must not starve the body it labels.
        budget = max(chunk_size - len(prefix), chunk_size // 2)
        units: list[str] = []
        for paragraph in paragraphs:
            if len(paragraph) <= budget:
                units.append(paragraph)
            else:
                units.extend(_split_paragraph(paragraph, budget, overlap))
        chunks.extend(prefix + body for body in _pack(units, budget, "\n\n"))
    return chunks
