"""ARB-A — the deterministic core: schema, compiler, evaluator.

No database, no fixtures, no I/O. If any test here needs one, the core has
stopped being the core.

The tests that matter most are not the happy paths. They are:

* `test_contains_is_refused_on_a_domain` and the `_domain_matches` family —
  the substring bypass this whole operator set exists to make unreachable.
* `test_platform_deny_beats_tenant_allow` — §5.3, the property that lets a
  platform baseline mean anything at all.
* `test_shadow_policy_never_returns_enforced` — the reason ARB-A is safe to
  ship to a running install.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.hxarbiter import (
    AttrSpec,
    AttrType,
    AttributeCatalog,
    CompileError,
    Effect,
    Evaluator,
    Obligation,
    ObligationKind,
    PolicyCompiler,
    PolicyMode,
    PolicyVersion,
    Rule,
    VELARIS_CATALOG,
    clamp,
)
from hxarbiter_core.evaluator import _domain_matches
from hxarbiter_core.schema import Comparison


def _compiler() -> PolicyCompiler:
    return PolicyCompiler(VELARIS_CATALOG)


def _version(rules: list[dict], version: int = 1) -> PolicyVersion:
    return PolicyVersion(
        policy_id=uuid.uuid4(),
        version=version,
        rules=[Rule.model_validate(r) for r in rules],
    )


def _compiled(rules: list[dict], *, tenant_id=None, mode=PolicyMode.ENFORCE,
              namespace="ai", policy_id=None):
    v = _version(rules)
    return _compiler().compile_version(
        policy_id=policy_id or v.policy_id,
        tenant_id=tenant_id,
        namespace=namespace,
        version=v,
        mode=mode,
    )


# ── the domain bypass class ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,pattern,expected",
    [
        ("example.com", "example.com", True),
        ("api.example.com", "example.com", True),
        ("a.b.example.com", "example.com", True),
        # The whole point. A suffix check without a label boundary says True.
        ("evil-example.com", "example.com", False),
        ("notexample.com", "example.com", False),
        # And the other direction: a longer domain that merely starts the same.
        ("example.com.attacker.net", "example.com", False),
        ("EXAMPLE.COM", "example.com", True),      # case-insensitive
        ("example.com.", "example.com", True),     # trailing root dot
        ("", "example.com", False),
        ("example.com", "", False),
        (None, "example.com", False),
    ],
)
def test_domain_matches_on_label_boundaries(host, pattern, expected):
    assert _domain_matches(host, pattern) is expected


def test_contains_is_refused_on_a_domain():
    """`contains` on a hostname must not be expressible at all."""
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "context.target_domain",
                          "op": "contains", "value": "example.com"},
        }])
    )
    assert not report.ok
    assert "bypass class" in " ".join(report.errors)


def test_matches_domain_requires_a_domain_attribute():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "context.purpose",
                          "op": "matches_domain", "value": "example.com"},
        }])
    )
    assert not report.ok


# ── the compiler refuses what would silently never match ─────────────────────


def test_unknown_attribute_is_a_compile_error():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.regoin", "op": "eq", "value": "eu"},
        }])
    )
    assert not report.ok
    assert "unknown attribute" in report.errors[0]


def test_value_outside_a_closed_set_is_refused():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "context.provider_class",
                          "op": "eq", "value": "EXTERNAL"},
        }])
    )
    assert not report.ok
    assert "not a permitted value" in report.errors[0]


def test_ordering_operator_on_a_string_is_refused():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.region", "op": "gt", "value": "eu"},
        }])
    )
    assert not report.ok


def test_boolean_where_a_number_belongs_is_refused():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "subject.mfa_age_seconds",
                          "op": "gt", "value": True},
        }])
    )
    assert not report.ok
    assert "boolean" in report.errors[0]


def test_empty_list_is_refused():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.region", "op": "in", "value": []},
        }])
    )
    assert not report.ok


def test_unary_operator_rejects_a_value():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "consent.basis",
                          "op": "is_present", "value": "consent"},
        }])
    )
    assert not report.ok


def test_depth_limit_is_enforced():
    cond = {"attr": "tenant.region", "op": "eq", "value": "eu-west"}
    for _ in range(12):
        cond = {"all": [cond]}
    report = _compiler().check_version(
        _version([{"id": "r1", "effect": "deny", "condition": cond}])
    )
    assert not report.ok
    assert "deeper than" in report.errors[0]


def test_attribute_to_attribute_type_mismatch_is_refused():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.region", "op": "eq",
                          "value": {"attr": "subject.mfa_age_seconds"}},
        }])
    )
    assert not report.ok
    assert "cannot compare" in report.errors[0]


def test_required_attrs_records_both_sides():
    report = _compiler().check_version(
        _version([{
            "id": "r1", "effect": "deny",
            "condition": {"attr": "resource.region", "op": "ne",
                          "value": {"attr": "tenant.region"}},
        }])
    )
    assert report.ok
    assert report.required_attrs == {"resource.region", "tenant.region"}


def test_compiler_collects_every_error_not_just_the_first():
    report = _compiler().check_version(
        _version([
            {"id": "r1", "effect": "deny",
             "condition": {"attr": "nope.here", "op": "eq", "value": "x"}},
            {"id": "r2", "effect": "deny",
             "condition": {"attr": "also.nope", "op": "eq", "value": "x"}},
        ])
    )
    assert not report.ok
    assert len(report.errors) == 2


def test_a_deny_rule_cannot_carry_obligations():
    with pytest.raises(Exception):
        Rule.model_validate({
            "id": "r1", "effect": "deny",
            "condition": {"attr": "tenant.region", "op": "eq", "value": "eu"},
            "obligations": [{"kind": "pseudonymize"}],
        })


def test_duplicate_rule_ids_are_refused():
    with pytest.raises(Exception):
        _version([
            {"id": "r1", "effect": "allow",
             "condition": {"attr": "tenant.region", "op": "eq", "value": "eu"}},
            {"id": "r1", "effect": "deny",
             "condition": {"attr": "tenant.region", "op": "eq", "value": "us"}},
        ])


# ── evaluation semantics ─────────────────────────────────────────────────────


def _evaluate(policies, attrs, default=Effect.DENY, ceiling=PolicyMode.ENFORCE):
    return Evaluator().evaluate(
        policies=policies, attrs=attrs,
        namespace_default=default, namespace_max_mode=ceiling,
    )


def test_namespace_default_applies_when_nothing_matches():
    d = _evaluate([], {"tenant.region": "eu-west"})
    assert d.effect is Effect.DENY
    assert d.default_applied is True
    assert d.matched == []


def test_deny_overrides_a_matching_allow():
    p = _compiled([
        {"id": "a", "effect": "allow",
         "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}},
        {"id": "d", "effect": "deny",
         "condition": {"attr": "context.provider_class",
                       "op": "eq", "value": "external"}},
    ])
    d = _evaluate([p], {"tenant.region": "eu-west",
                        "context.provider_class": "external"})
    assert d.effect is Effect.DENY
    # The explanation names only what caused the deny.
    assert [m.rule_id for m in d.matched] == ["d"]


def test_platform_deny_beats_tenant_allow():
    """§5.3 — a tenant can tighten the platform baseline, never loosen it."""
    platform = _compiled(
        [{"id": "pd", "effect": "deny",
          "condition": {"attr": "context.provider_class",
                        "op": "eq", "value": "external"}}],
        tenant_id=None,
    )
    tenant = _compiled(
        [{"id": "ta", "effect": "allow",
          "condition": {"attr": "context.provider_class",
                        "op": "eq", "value": "external"}}],
        tenant_id="acme",
    )
    for order in ([platform, tenant], [tenant, platform]):
        d = _evaluate(order, {"context.provider_class": "external"})
        assert d.effect is Effect.DENY, "order must not change the answer"
        assert [m.rule_id for m in d.matched] == ["pd"]


def test_obligations_from_every_matching_allow_are_unioned():
    p = _compiled([
        {"id": "a1", "effect": "allow",
         "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
         "obligations": [{"kind": "pseudonymize"}]},
        {"id": "a2", "effect": "allow",
         "condition": {"attr": "context.provider_class",
                       "op": "eq", "value": "local"},
         "obligations": [{"kind": "record_purpose"}, {"kind": "pseudonymize"}]},
    ])
    d = _evaluate([p], {"tenant.region": "eu-west",
                        "context.provider_class": "local"})
    assert d.effect is Effect.ALLOW
    kinds = sorted(o.kind for o in d.obligations)
    assert kinds == [ObligationKind.PSEUDONYMIZE, ObligationKind.RECORD_PURPOSE]
    assert len(d.obligations) == 2, "duplicates must collapse"


def test_a_missing_attribute_never_satisfies_a_rule():
    """An unresolved attribute makes a comparison false, never true."""
    p = _compiled([{
        "id": "a", "effect": "allow",
        "condition": {"attr": "consent.basis", "op": "eq", "value": "consent"},
    }])
    d = _evaluate([p], {})            # consent.basis never resolved
    assert d.effect is Effect.DENY    # falls through to the namespace default
    assert d.default_applied is True


def test_is_absent_distinguishes_unresolved_from_null():
    p = _compiled([{
        "id": "a", "effect": "allow",
        "condition": {"attr": "consent.basis", "op": "is_absent"},
    }])
    assert _evaluate([p], {}).effect is Effect.ALLOW
    assert _evaluate([p], {"consent.basis": None}).effect is Effect.ALLOW
    assert _evaluate([p], {"consent.basis": "consent"}).effect is Effect.DENY


def test_intersects_and_subset_on_data_classes():
    p = _compiled([{
        "id": "d", "effect": "deny",
        "condition": {"attr": "resource.data_classes",
                      "op": "intersects", "value": ["special_category", "health"]},
    }])
    assert _evaluate([p], {"resource.data_classes": ["name", "health"]}).effect is Effect.DENY
    assert _evaluate([p], {"resource.data_classes": ["name"]}).effect is Effect.DENY  # default
    assert _evaluate([p], {"resource.data_classes": ["name"]}).default_applied is True


def test_a_bad_runtime_type_does_not_raise():
    """One bad PIP value must not take down every decision in the namespace."""
    p = _compiled([{
        "id": "d", "effect": "deny",
        "condition": {"attr": "subject.mfa_age_seconds", "op": "gt", "value": 300},
    }])
    d = _evaluate([p], {"subject.mfa_age_seconds": "not a number"})
    assert d.effect is Effect.DENY          # namespace default, not an exception
    assert d.default_applied is True


# ── rollout modes: why ARB-A is safe on a running install ────────────────────


def test_shadow_policy_never_returns_enforced():
    p = _compiled([{
        "id": "d", "effect": "deny",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
    }], mode=PolicyMode.SHADOW)
    d = _evaluate([p], {"tenant.region": "eu-west"})
    assert d.effect is Effect.DENY
    assert d.enforced is False, "a shadow policy must never bind"


def test_namespace_ceiling_holds_an_enforce_policy_down():
    p = _compiled([{
        "id": "d", "effect": "deny",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
    }], mode=PolicyMode.ENFORCE)
    d = _evaluate([p], {"tenant.region": "eu-west"}, ceiling=PolicyMode.SHADOW)
    assert d.enforced is False


def test_a_shadow_policy_does_not_make_a_matching_enforce_policy_unbinding():
    shadow = _compiled([{
        "id": "s", "effect": "deny",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
    }], mode=PolicyMode.SHADOW, policy_id=uuid.uuid4())
    live = _compiled([{
        "id": "e", "effect": "deny",
        "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
    }], mode=PolicyMode.ENFORCE, policy_id=uuid.uuid4())
    d = _evaluate([shadow, live], {"tenant.region": "eu-west"})
    assert d.enforced is True


def test_clamp_takes_the_weaker_mode():
    assert clamp(PolicyMode.ENFORCE, PolicyMode.SHADOW) is PolicyMode.SHADOW
    assert clamp(PolicyMode.SHADOW, PolicyMode.ENFORCE) is PolicyMode.SHADOW
    assert clamp(PolicyMode.ENFORCE, PolicyMode.ENFORCE) is PolicyMode.ENFORCE
    assert clamp(PolicyMode.DRY_RUN, PolicyMode.ENFORCE) is PolicyMode.DRY_RUN


# ── determinism and signing ──────────────────────────────────────────────────


def test_signature_is_stable_across_key_order():
    a = PolicyVersion(policy_id=uuid.UUID(int=7), version=1, rules=[
        Rule.model_validate({
            "id": "r1", "effect": "deny", "description": "x",
            "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
        })])
    b = PolicyVersion(policy_id=uuid.UUID(int=7), version=1, rules=[
        Rule.model_validate({
            "effect": "deny", "condition": {
                "value": "eu-west", "op": "eq", "attr": "tenant.region"},
            "description": "x", "id": "r1",
        })])
    assert a.signature() == b.signature()


def test_signature_changes_when_a_rule_changes():
    a = _version([{"id": "r1", "effect": "deny",
                   "condition": {"attr": "tenant.region", "op": "eq", "value": "eu-west"}}])
    b = PolicyVersion(policy_id=a.policy_id, version=1, rules=[
        Rule.model_validate({"id": "r1", "effect": "allow",
                             "condition": {"attr": "tenant.region", "op": "eq",
                                           "value": "eu-west"}})])
    assert a.signature() != b.signature()


def test_evaluation_is_repeatable():
    p = _compiled([{
        "id": "d", "effect": "deny",
        "condition": {"all": [
            {"attr": "tenant.region", "op": "eq", "value": "eu-west"},
            {"any": [
                {"attr": "context.provider_class", "op": "eq", "value": "external"},
                {"attr": "resource.data_classes", "op": "contains", "value": "health"},
            ]},
            {"not": {"attr": "consent.basis", "op": "eq", "value": "vital_interests"}},
        ]},
    }])
    attrs = {"tenant.region": "eu-west", "context.provider_class": "external",
             "resource.data_classes": ["name"], "consent.basis": "consent"}
    first = _evaluate([p], attrs)
    for _ in range(50):
        again = _evaluate([p], attrs)
        assert again.effect is first.effect
        assert [m.rule_id for m in again.matched] == [m.rule_id for m in first.matched]


# ── the worked example from the design doc, Appendix A ───────────────────────


def test_worked_example_eu_tenant_external_ai_is_denied():
    """*EU tenants' personal data must never be processed by external AI,
    regardless of consent.*"""
    p = _compiled([{
        "id": "r1",
        "description": "EU personal data may not reach an external provider",
        "effect": "deny",
        "condition": {"all": [
            {"attr": "tenant.region", "op": "in", "value": ["eu-west", "eu-central"]},
            {"attr": "context.provider_class", "op": "eq", "value": "external"},
            {"attr": "resource.data_classes", "op": "intersects",
             "value": ["personal", "special_category"]},
        ]},
    }])

    denied = _evaluate([p], {
        "tenant.region": "eu-west",
        "context.provider_class": "external",
        "resource.data_classes": ["personal"],
        "consent.basis": "consent",     # consent must NOT rescue it
    })
    assert denied.effect is Effect.DENY
    assert denied.default_applied is False

    # A local provider on the same data is not caught by this rule; the
    # namespace default (deny for ai.*) still applies until something allows it.
    local = _evaluate([p], {
        "tenant.region": "eu-west",
        "context.provider_class": "local",
        "resource.data_classes": ["personal"],
    })
    assert local.default_applied is True


def test_custom_catalog_keeps_the_core_platform_agnostic():
    """§18.3 — a host platform supplies its own catalog; the core does not care."""
    cat = AttributeCatalog([
        AttrSpec(path="thing.colour", type=AttrType.STRING, allowed=("red", "blue")),
    ])
    v = _version([{"id": "r1", "effect": "deny",
                   "condition": {"attr": "thing.colour", "op": "eq", "value": "red"}}])
    compiled = PolicyCompiler(cat).compile_version(
        policy_id=v.policy_id, tenant_id=None, namespace="thing",
        version=v, mode=PolicyMode.ENFORCE)
    assert _evaluate([compiled], {"thing.colour": "red"}).effect is Effect.DENY
    # And Velaris' own attributes are unknown to it.
    with pytest.raises(CompileError):
        PolicyCompiler(cat).compile_version(
            policy_id=v.policy_id, tenant_id=None, namespace="ai",
            version=_version([{"id": "r1", "effect": "deny",
                               "condition": {"attr": "tenant.region", "op": "eq",
                                             "value": "eu-west"}}]),
            mode=PolicyMode.ENFORCE)


# ── attribute-to-attribute comparison ────────────────────────────────────────


def test_a_condition_can_compare_two_attributes():
    """`AttrRef` was declared, documented on `Comparison.value`, exported from
    nowhere and tested by nothing — so a 2026-08-29 audit initially reported it
    as unimplemented. It is implemented, in both halves; what was missing was
    any evidence of that.

    It matters commercially as well as technically: it is what lets the
    archetypal rule be written without naming a jurisdiction, so the product
    reads as a policy engine rather than an EU compliance tool.
    """
    from hxarbiter_core import (AttrSpec, AttrType, AttributeCatalog, Effect,
                                Evaluator, PolicyCompiler, PolicyMode,
                                PolicyVersion, Rule)
    import uuid as _uuid

    catalog = AttributeCatalog([
        AttrSpec(path="tenant.region", type=AttrType.STRING),
        AttrSpec(path="resource.region", type=AttrType.STRING),
    ])
    rule = Rule.model_validate({
        "id": "stay-in-region", "effect": "deny",
        "condition": {"attr": "resource.region", "op": "ne",
                      "value": {"attr": "tenant.region"}}})
    version = PolicyVersion(policy_id=_uuid.uuid4(), version=1, rules=[rule])

    report = PolicyCompiler(catalog).check_version(version)
    assert report.ok
    assert report.required_attrs >= {"tenant.region", "resource.region"}, (
        "the right-hand attribute must be declared as required, or no PIP "
        "resolves it and the rule silently never fires")

    compiled = PolicyCompiler(catalog).compile_version(
        policy_id=version.policy_id, tenant_id=None, namespace="ai",
        version=version, mode=PolicyMode.ENFORCE)
    ev = Evaluator()
    assert ev.evaluate(policies=[compiled],
                       attrs={"tenant.region": "a", "resource.region": "b"},
                       namespace_default=Effect.ALLOW,
                       namespace_max_mode=PolicyMode.ENFORCE).effect is Effect.DENY
    assert ev.evaluate(policies=[compiled],
                       attrs={"tenant.region": "a", "resource.region": "a"},
                       namespace_default=Effect.ALLOW,
                       namespace_max_mode=PolicyMode.ENFORCE).effect is Effect.ALLOW


def test_comparing_attributes_of_different_types_is_refused_at_compile_time():
    """Silently evaluating to False for the life of the policy is the worse
    outcome: the rule looks active and catches nothing."""
    from hxarbiter_core import (AttrSpec, AttrType, AttributeCatalog,
                                CompileError, PolicyCompiler, PolicyVersion, Rule)
    import uuid as _uuid

    catalog = AttributeCatalog([
        AttrSpec(path="tenant.region", type=AttrType.STRING),
        AttrSpec(path="subject.roles", type=AttrType.STRING_SET),
    ])
    version = PolicyVersion(policy_id=_uuid.uuid4(), version=1, rules=[
        Rule.model_validate({
            "id": "mixed", "effect": "deny",
            "condition": {"attr": "tenant.region", "op": "eq",
                          "value": {"attr": "subject.roles"}}})])
    report = PolicyCompiler(catalog).check_version(version)
    assert not report.ok
