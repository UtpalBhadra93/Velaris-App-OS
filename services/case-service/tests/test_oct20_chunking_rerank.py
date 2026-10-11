"""Oct item 20 — boundary-aware chunking and a local rerank for document Q&A.

The claims worth a test are the ones an upgrade depends on:

* unset means **exactly** what an install did before — same chunks, same order;
* an unrecognised value is "off", never an error;
* rows cut by the old chunker are still found and answered with the new
  strategy switched on;
* the rerank changes the order and nothing else — `score` stays the cosine the
  egress floor is defined against — and it never calls the backend.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from velaris_shared.config import get_settings
from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel, DocumentChunkModel
from case_service.hxnexus.chunker import chunk_text, resolve_strategy
from case_service.hxnexus.rerank import candidate_count, rerank_lexical, resolve_mode
from case_service.hxnexus.service import index_document, qa_over_documents


def _old_window(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """The chunker as it shipped before item 20, kept here verbatim."""
    text = text.strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


POLICY = """# Home Insurance Policy

This policy is issued to the named policyholder and covers the insured property.

## Cover

1. Buildings

We cover loss or damage to the buildings caused by fire, storm or flood. The most we will pay is the sum insured shown in the schedule.

2. Contents

We cover household goods and personal possessions while they are in the home.

## Cancellation

You may cancel within 14 days of receiving the documents. We will refund the premium unless a claim has been made.

EXCLUSIONS

We do not cover wear and tear. We do not cover damage caused by pets.
"""


def _settings(**over):
    return get_settings().model_copy(update=over)


def _backend(embed=None):
    b = MagicMock()
    b.backend_name = "mock"
    b.available = True
    b.is_external = False
    b.complete = AsyncMock(return_value="answer")
    b.embed = AsyncMock(return_value=embed or [1.0, 0.0])
    return b


@pytest_asyncio.fixture
async def case(session):
    ct = CaseTypeModel(name=f"Oct20-{uuid.uuid4().hex[:6]}", version="1.0",
                       lifecycle_process_id="lp-oct20", definition_json={"stages": []})
    session.add(ct)
    await session.commit()
    c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0",
                          status="open", priority="high", data={})
    session.add(c)
    await session.commit()
    return c


# ── the default is the old behaviour ──────────────────────────────────

@pytest.mark.parametrize("strategy", [None, "", "window", "WINDOW", "  ", "nonsense"])
def test_unset_or_unknown_strategy_is_the_old_chunker(strategy):
    for text in (POLICY, "A" * 1300, "one short line", "", "   "):
        assert chunk_text(text, strategy=strategy) == _old_window(text)


@pytest.mark.parametrize("value", [None, "", "off", "OFF", "none", "false", "0", "cross-encoder"])
def test_unset_or_unknown_rerank_is_off(value):
    assert resolve_mode(value) == "off"


def test_shipped_defaults_are_off():
    s = get_settings()
    assert resolve_strategy(s.hxnexus_chunk_strategy) == "window"
    assert resolve_mode(s.hxnexus_rerank) == "off"


# ── structure strategy ────────────────────────────────────────────────

def test_structure_keeps_the_heading_with_its_section():
    chunks = chunk_text(POLICY, strategy="structure")
    cancel = [c for c in chunks if "14 days" in c]
    assert len(cancel) == 1
    assert cancel[0].startswith("Cancellation\n")
    excl = [c for c in chunks if "wear and tear" in c]
    assert excl and excl[0].startswith("EXCLUSIONS\n")


def test_structure_never_cuts_a_section_into_its_neighbour():
    for c in chunk_text(POLICY, strategy="structure"):
        assert not ("14 days" in c and "wear and tear" in c)
        assert not ("household goods" in c and "14 days" in c)


def test_structure_nests_a_heading_under_its_parent():
    chunks = chunk_text(POLICY, strategy="structure")
    assert any(c.startswith("Cover > 1. Buildings\n") and "fire, storm" in c for c in chunks)
    # the parent does not stick once the child section has had its body
    assert any(c.startswith("2. Contents\n") for c in chunks)


def test_structure_loses_no_text():
    words = set(POLICY.replace("#", " ").split())
    got = set(" ".join(chunk_text(POLICY, strategy="structure")).split())
    assert words - got == set()


def test_structure_splits_a_long_paragraph_on_sentences():
    para = " ".join(f"Sentence number {i} ends here." for i in range(60))
    chunks = chunk_text(para, chunk_size=200, strategy="structure")
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert all(c.endswith(".") for c in chunks)


def test_structure_falls_back_to_the_window_for_an_unbreakable_run():
    chunks = chunk_text("X" * 1500, chunk_size=200, overlap=20, strategy="structure")
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    assert sum(len(c) for c in chunks) >= 1500


def test_structure_respects_chunk_size_with_a_heading():
    body = " ".join(f"Clause {i} applies to the insured." for i in range(80))
    chunks = chunk_text(f"## Conditions\n\n{body}", chunk_size=300, strategy="structure")
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert all(c.startswith("Conditions\n") for c in chunks)


def test_structure_does_not_take_a_sentence_for_a_heading():
    text = "The claim was settled.\n\nPayment followed in March.\n\nIt was 1200 pounds"
    chunks = chunk_text(text, strategy="structure")
    assert len(chunks) == 1 and "\n\n" in chunks[0]


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n"])
def test_structure_empty(text):
    assert chunk_text(text, strategy="structure") == []


def test_structure_headings_only_is_still_indexed():
    assert chunk_text("# Title\n\n## Subtitle\n", strategy="structure") != []


# ── rerank ────────────────────────────────────────────────────────────

def _c(cid, text, score):
    return {"chunk_id": cid, "text": text, "score": score, "metadata": {}}


def test_rerank_lifts_the_exact_term():
    chunks = [
        _c("a", "General information about policies and cover.", 0.62),
        _c("b", "Other general remarks about insurance.", 0.60),
        _c("c", "Policy number HX-99812 was issued in March.", 0.55),
    ]
    out = rerank_lexical("What is policy HX-99812?", chunks, top_k=2)
    assert out[0]["chunk_id"] == "c"
    assert len(out) == 2


def test_rerank_leaves_score_as_cosine():
    chunks = [_c("a", "alpha beta", 0.5), _c("b", "gamma policy", 0.4)]
    out = rerank_lexical("policy", chunks, top_k=2)
    assert {c["chunk_id"]: c["score"] for c in out} == {"a": 0.5, "b": 0.4}
    assert all("rerank_score" in c for c in out)


def test_rerank_with_no_matching_term_is_the_cosine_order():
    chunks = [_c("a", "alpha", 0.9), _c("b", "beta", 0.8), _c("c", "gamma", 0.7)]
    assert rerank_lexical("zzz", chunks, top_k=2) == chunks[:2]
    assert rerank_lexical("?!", chunks, top_k=2) == chunks[:2]


@pytest.mark.parametrize("weight", [0, -1, "x", None])
def test_rerank_zero_or_bad_weight_is_the_cosine_order(weight):
    chunks = [_c("a", "alpha", 0.9), _c("b", "policy", 0.1)]
    assert rerank_lexical("policy", chunks, top_k=2, weight=weight) == chunks


def test_rerank_empty():
    assert rerank_lexical("q", [], top_k=5) == []


def test_candidate_count_never_below_top_k():
    assert candidate_count(5, 20) == 20
    assert candidate_count(5, 2) == 5
    assert candidate_count(5, "bad") == 5


# ── through the service ───────────────────────────────────────────────

async def _seed(session, case, rows):
    """rows: [(text, embedding)] — written as an old install would have."""
    doc_id = uuid.uuid4()
    for i, (text, emb) in enumerate(rows):
        session.add(DocumentChunkModel(id=uuid.uuid4(), document_id=doc_id, case_id=case.id,
                                       chunk_index=i, chunk_text=text, embedding=emb,
                                       tenant_id=None))
    await session.commit()


def _context(backend) -> str:
    return backend.complete.call_args.args[0]


ROWS = [
    ("General information about policies and cover.", [1.0, 0.05]),
    ("Other general remarks about insurance.", [1.0, 0.10]),
    ("More general text on the subject of cover.", [1.0, 0.15]),
    ("Policy number HX-99812 was issued in March.", [1.0, 0.60]),
]


@pytest.mark.asyncio
async def test_qa_default_is_cosine_top_k(session, case):
    await _seed(session, case, ROWS)
    b = _backend()
    res = await qa_over_documents(session, case.id, "What is policy HX-99812?",
                                  tenant_id=None, top_k=2, backend=b)
    assert len(res["sources"]) == 2
    assert "HX-99812 was issued" not in _context(b)
    scores = [s["score"] for s in res["sources"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_qa_rerank_brings_the_exact_match_in(session, case):
    await _seed(session, case, ROWS)
    b = _backend()
    with patch("velaris_shared.config.get_settings",
               return_value=_settings(hxnexus_rerank="lexical")):
        res = await qa_over_documents(session, case.id, "What is policy HX-99812?",
                                      tenant_id=None, top_k=2, backend=b)
    assert len(res["sources"]) == 2
    assert "HX-99812 was issued" in _context(b)
    # one embed for the question, one completion — the rerank made no call
    assert b.embed.await_count == 1 and b.complete.await_count == 1


@pytest.mark.asyncio
async def test_qa_rerank_external_still_capped_and_floored(session, case):
    """The egress layers apply to what survives the rerank, unchanged."""
    await _seed(session, case, ROWS + [("Policy HX-99812 unrelated vector.", [0.0, 1.0])])
    b = _backend()
    b.is_external = True
    b.last_route = "local"
    cfg = _settings(hxnexus_rerank="lexical", ai_egress_top_k=2, ai_egress_min_score=0.5)
    with patch("velaris_shared.config.get_settings", return_value=cfg):
        res = await qa_over_documents(session, case.id, "What is policy HX-99812?",
                                      tenant_id=None, top_k=5, backend=b)
    assert 1 <= len(res["sources"]) <= 2
    assert all(s["score"] >= 0.5 for s in res["sources"])
    assert "unrelated vector" not in _context(b)


@pytest.mark.asyncio
async def test_old_chunks_answer_with_the_new_strategy_on(session, case):
    """Switching the strategy re-indexes nothing and hides nothing."""
    await _seed(session, case, [(c, [1.0, 0.0]) for c in _old_window(POLICY)])
    before = len(_old_window(POLICY))
    b = _backend()
    cfg = _settings(hxnexus_chunk_strategy="structure", hxnexus_rerank="lexical")
    with patch("velaris_shared.config.get_settings", return_value=cfg):
        res = await qa_over_documents(session, case.id, "When may I cancel?",
                                      tenant_id=None, backend=b)
        n = await index_document(session, uuid.uuid4(), case.id, POLICY.encode(),
                                 "text/plain", None, backend=b)
    assert res["sources"]
    from sqlalchemy import select
    rows = (await session.execute(select(DocumentChunkModel).where(
        DocumentChunkModel.case_id == case.id))).scalars().all()
    assert len(rows) == before + n
    assert any(r.chunk_text.startswith("Cancellation\n") for r in rows)


@pytest.mark.asyncio
async def test_index_default_writes_the_old_chunks(session, case):
    b = _backend()
    doc = uuid.uuid4()
    await index_document(session, doc, case.id, POLICY.encode(), "text/plain", None, backend=b)
    from sqlalchemy import select
    rows = (await session.execute(select(DocumentChunkModel).where(
        DocumentChunkModel.document_id == doc).order_by(DocumentChunkModel.chunk_index)
    )).scalars().all()
    assert [r.chunk_text for r in rows] == _old_window(POLICY)


# ── a bad number in .env must not stop the service booting ────────────

@pytest.mark.parametrize("raw", ["", "  ", "abc", "1e", "None"])
def test_unreadable_rerank_numbers_fall_back_to_the_default(monkeypatch, raw):
    from velaris_shared.config import Settings
    monkeypatch.setenv("VELARIS_CASE_HXNEXUS_RERANK_CANDIDATES", raw)
    monkeypatch.setenv("VELARIS_CASE_HXNEXUS_RERANK_LEXICAL_WEIGHT", raw)
    s = Settings()
    assert s.hxnexus_rerank_candidates == 20
    assert s.hxnexus_rerank_lexical_weight == 0.3


def test_readable_rerank_numbers_are_kept(monkeypatch):
    from velaris_shared.config import Settings
    monkeypatch.setenv("VELARIS_CASE_HXNEXUS_RERANK_CANDIDATES", " 40 ")
    monkeypatch.setenv("VELARIS_CASE_HXNEXUS_RERANK_LEXICAL_WEIGHT", "0.5")
    s = Settings()
    assert s.hxnexus_rerank_candidates == 40
    assert s.hxnexus_rerank_lexical_weight == 0.5
