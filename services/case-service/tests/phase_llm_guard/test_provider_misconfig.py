"""A provider that cannot possibly work must never be resolved.

`AI_PROVIDER` set without `AI_API_KEY` used to build a fully-formed external
backend holding an empty key. Nothing refused it: the platform reported the
provider as configured, every completion failed at the provider with a 401 —
which reads as a model outage rather than a typo in `.env` — and the local
Ollama that would have answered was never consulted, because the universal
branch returned before reaching the fallback.

These tests pin both halves of the fix: the resolver falls back to local, and
`get_ai_info()` says *why* rather than quietly reporting a healthy local
install.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from case_service.hxnexus import factory
from case_service.hxnexus.ollama_backend import OllamaBackend


def _settings(**over) -> SimpleNamespace:
    """Settings stand-in carrying only the fields the factory reads."""
    base = dict(
        ai_provider="", ai_api_key="", ai_model="", ai_embed_model="", ai_base_url="",
        ai_backend="ollama",
        ai_ollama_url="http://localhost:11434",
        ai_ollama_model="llama3.2",
        ai_ollama_embed_model="nomic-embed-text",
        ai_openai_api_key="", ai_openai_model="gpt-4o-mini",
        ai_openai_embed_model="text-embedding-3-small",
        ai_anthropic_api_key="", ai_anthropic_model="claude-haiku-4-5-20251001",
        ai_egress_policy="minimized",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def with_settings(monkeypatch):
    """Point the factory at a synthetic settings object.

    `_resolve_llm_backend` imports `get_settings` inside the function body, so
    the patch has to land on the config module rather than on the factory.
    """
    def _apply(**over):
        s = _settings(**over)
        monkeypatch.setattr("case_service.config.get_settings", lambda: s)
        return s
    return _apply


def _unwrap(backend):
    """Strip GuardedBackend / EgressGuardedBackend down to the real backend."""
    inner = getattr(backend, "_inner", backend)
    return getattr(inner, "_external", inner)


# ─── The fault detector ───────────────────────────────────────────

def test_external_provider_without_a_key_is_a_fault(with_settings):
    s = with_settings(ai_provider="anthropic")
    fault = factory.detect_provider_misconfig(s)
    assert fault is not None
    assert fault["provider"] == "anthropic"
    assert fault["missing"] == "VELARIS_CASE_AI_API_KEY"


def test_a_whitespace_only_key_does_not_count_as_configured(with_settings):
    s = with_settings(ai_provider="groq", ai_api_key="   ")
    assert factory.detect_provider_misconfig(s) is not None


def test_a_configured_provider_is_not_a_fault(with_settings):
    s = with_settings(ai_provider="groq", ai_api_key="gsk_live")
    assert factory.detect_provider_misconfig(s) is None


def test_ollama_and_unset_are_never_faults(with_settings):
    assert factory.detect_provider_misconfig(with_settings()) is None
    assert factory.detect_provider_misconfig(with_settings(ai_provider="ollama")) is None


def test_custom_needs_an_address_not_a_key(with_settings):
    """`custom` is the local-runtime hatch — LM Studio and vLLM want no key."""
    keyless = with_settings(ai_provider="custom", ai_base_url="http://localhost:1234/v1")
    assert factory.detect_provider_misconfig(keyless) is None

    addressless = with_settings(ai_provider="custom")
    fault = factory.detect_provider_misconfig(addressless)
    assert fault is not None
    assert fault["missing"] == "VELARIS_CASE_AI_BASE_URL"


def test_an_unknown_provider_name_is_refused_rather_than_defaulted(with_settings):
    """The dangerous one: OpenAIBackend defaults its own base URL, so a typo'd
    provider name with a real key posted that key to api.openai.com."""
    s = with_settings(ai_provider="gorq", ai_api_key="gsk_live")
    fault = factory.detect_provider_misconfig(s)
    assert fault is not None
    assert fault["missing"] == "VELARIS_CASE_AI_BASE_URL"


def test_an_unknown_provider_with_an_explicit_address_is_allowed(with_settings):
    s = with_settings(ai_provider="gorq", ai_api_key="k", ai_base_url="https://internal/v1")
    assert factory.detect_provider_misconfig(s) is None


# ─── The resolver falls back ──────────────────────────────────────

def test_keyless_provider_resolves_to_the_local_backend(with_settings, caplog):
    with_settings(ai_provider="anthropic")
    with caplog.at_level("ERROR"):
        backend = factory._resolve_llm_backend()

    assert isinstance(backend, OllamaBackend)
    assert backend.is_external is False
    # The operator has to be able to find this from the log alone.
    assert "VELARIS_CASE_AI_API_KEY" in caplog.text


def test_a_configured_provider_still_resolves_externally(with_settings):
    """The guard must not cost a correctly configured install its provider."""
    with_settings(ai_provider="groq", ai_api_key="gsk_live", ai_egress_policy="full")
    backend = _unwrap(factory._resolve_llm_backend())

    assert backend.backend_name == "groq"
    assert backend.is_external is True


def test_fallback_leaves_the_legacy_vars_alone(with_settings):
    """A keyless universal provider falls back to local — it must not silently
    promote a legacy provider-specific key the operator did not select."""
    with_settings(ai_provider="anthropic", ai_openai_api_key="sk-legacy")
    assert isinstance(factory._resolve_llm_backend(), OllamaBackend)


# ─── The status surface says why ──────────────────────────────────

def test_get_ai_info_reports_the_fault(with_settings):
    with_settings(ai_provider="anthropic")
    info = factory.get_ai_info()

    # `backend` alone reads as a deliberate local-only install; this is what
    # distinguishes that from a dropped provider.
    assert info["backend"] == "ollama"
    assert info["misconfigured"]["provider"] == "anthropic"
    assert info["misconfigured"]["missing"] == "VELARIS_CASE_AI_API_KEY"


def test_get_ai_info_reports_no_fault_when_healthy(with_settings):
    with_settings(ai_provider="groq", ai_api_key="gsk_live")
    assert factory.get_ai_info()["misconfigured"] is None
