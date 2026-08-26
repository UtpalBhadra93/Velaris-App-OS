"""The LLM core — provider backends, the egress guard and its audit trail.

Lifted out of `case_service/hxnexus/` at 13c. Nothing in here ever imported the
application: the eight modules read `velaris_shared.config` and write
`velaris_shared.db` models and nothing else, which is why the move was possible
at all. It had to happen for analytics-service to leave, since the natural-
language query path (`analytics/query_engine.nl_query`) calls the provider
directly — the alternatives were splitting that module in half or putting an
AI round-trip behind another REST hop.

What stayed in `case_service.hxnexus` is the part that is genuinely about this
application's documents and cases: the RAG service, case Q&A, autodoc, the
vector store, chunking and text extraction.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from .factory import check_ai_available, generate_json, get_llm_backend

__all__ = ["check_ai_available", "generate_json", "get_llm_backend"]
