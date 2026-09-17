"""ARB — load and determinism, against §13's targets.

Two things are measured here and one is asserted hard:

* **p99 of pure evaluation < 2 ms** (§13, excluding PIPs). This is the number
  that decides whether a PEP can sit in front of ordinary request handling at
  all. It is asserted with headroom rather than at the line, because a test that
  fails on a busy machine gets deleted.
* **Determinism under load.** The same request against the same policy set must
  produce byte-identical decisions, every time, from any order. This is not a
  performance property — it is the product's central claim — so it is checked
  across thousands of evaluations rather than a handful.

Scale is deliberately unkind: 200 rules is the per-version ceiling the API
enforces, so this measures the worst policy set a tenant is allowed to have.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import statistics
import time
import uuid

import pytest

from case_service.hxarbiter import (
    Effect,
    Evaluator,
    PolicyCompiler,
    PolicyMode,
    Rule,
    VELARIS_CATALOG,
)
from hxarbiter_core import PolicyVersion

#: The API's own ceiling (`MAX_RULES_PER_VERSION`).
WORST_CASE_RULES = 200

#: §13 says p99 < 2 ms. Asserted at 5 ms: the target is the design goal, this is
#: the regression tripwire — a test that fails on a loaded CI box gets deleted,
#: and a deleted test measures nothing.
P99_CEILING_MS = 5.0


def _busy_rule(i: int) -> Rule:
    """A rule with real work in it — nesting, a set test, a domain match."""
    return Rule.model_validate({
        "id": f"r{i}",
        "description": f"rule {i}",
        "effect": "deny" if i % 7 == 0 else "allow",
        "condition": {"all": [
            {"attr": "tenant.region", "op": "in",
             "value": [f"region-{i}", "eu-west"]},
            {"any": [
                {"attr": "resource.data_classes", "op": "intersects",
                 "value": ["personal", f"class-{i}"]},
                {"attr": "context.target_domain", "op": "matches_domain",
                 "value": [f"host{i}.example.com", "api.example.com"]},
            ]},
            {"not": {"attr": "consent.basis", "op": "eq",
                     "value": "vital_interests"}},
        ]},
    })


@pytest.fixture(scope="module")
def heavy():
    rules = [_busy_rule(i) for i in range(WORST_CASE_RULES)]
    version = PolicyVersion(policy_id=uuid.uuid4(), version=1, rules=rules)
    compiled = PolicyCompiler(VELARIS_CATALOG).compile_version(
        policy_id=version.policy_id, tenant_id=None, namespace="ai",
        version=version, mode=PolicyMode.ENFORCE)
    return compiled


ATTRS = {
    "tenant.region": "eu-west",
    "resource.data_classes": ["personal", "health"],
    "context.target_domain": "api.example.com",
    "consent.basis": "consent",
}


def test_the_worst_allowed_policy_set_compiles():
    """200 rules × up to 128 nodes is what the API permits. It must compile."""
    rules = [_busy_rule(i) for i in range(WORST_CASE_RULES)]
    report = PolicyCompiler(VELARIS_CATALOG).check_version(
        PolicyVersion(policy_id=uuid.uuid4(), version=1, rules=rules))
    assert report.ok, report.errors


def test_p99_evaluation_stays_under_the_ceiling(heavy):
    """§13: p99 < 2 ms excluding PIPs, on the worst policy set allowed."""
    ev = Evaluator()
    samples: list[float] = []

    for _ in range(200):                      # warm the interpreter
        ev.evaluate(policies=[heavy], attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)

    for _ in range(3000):
        t0 = time.perf_counter()
        ev.evaluate(policies=[heavy], attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)
        samples.append((time.perf_counter() - t0) * 1000.0)

    samples.sort()
    p50 = samples[len(samples) // 2]
    p99 = samples[int(len(samples) * 0.99)]
    print(f"\n  200-rule policy set: p50={p50:.3f}ms p99={p99:.3f}ms "
          f"max={samples[-1]:.3f}ms mean={statistics.mean(samples):.3f}ms")
    assert p99 < P99_CEILING_MS, f"p99 {p99:.3f}ms exceeds {P99_CEILING_MS}ms"


def test_a_realistic_policy_set_is_an_order_of_magnitude_faster(heavy):
    """The number that matters in practice: a handful of rules, not 200."""
    rules = [_busy_rule(i) for i in range(5)]
    version = PolicyVersion(policy_id=uuid.uuid4(), version=1, rules=rules)
    compiled = PolicyCompiler(VELARIS_CATALOG).compile_version(
        policy_id=version.policy_id, tenant_id=None, namespace="ai",
        version=version, mode=PolicyMode.ENFORCE)

    ev = Evaluator()
    for _ in range(200):
        ev.evaluate(policies=[compiled], attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)

    samples = []
    for _ in range(3000):
        t0 = time.perf_counter()
        ev.evaluate(policies=[compiled], attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]
    print(f"  5-rule policy set:   p99={p99:.4f}ms")
    assert p99 < 1.0


def test_many_policies_across_scopes_stay_bounded():
    """Platform + tenant policies are evaluated together (§5.3)."""
    ev = Evaluator()
    compiled = []
    for scope in (None, "tenant-a"):
        for chunk in range(10):
            rules = [_busy_rule(i) for i in range(chunk * 5, chunk * 5 + 5)]
            v = PolicyVersion(policy_id=uuid.uuid4(), version=1, rules=rules)
            compiled.append(PolicyCompiler(VELARIS_CATALOG).compile_version(
                policy_id=v.policy_id, tenant_id=scope, namespace="ai",
                version=v, mode=PolicyMode.ENFORCE))

    for _ in range(100):
        ev.evaluate(policies=compiled, attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)

    samples = []
    for _ in range(2000):
        t0 = time.perf_counter()
        ev.evaluate(policies=compiled, attrs=ATTRS,
                    namespace_default=Effect.DENY,
                    namespace_max_mode=PolicyMode.ENFORCE)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]
    print(f"  20 policies, 2 scopes: p99={p99:.3f}ms")
    assert p99 < P99_CEILING_MS


# ── determinism: the product's central claim, at volume ─────────────────────


def test_ten_thousand_evaluations_are_byte_identical(heavy):
    ev = Evaluator()
    first = ev.evaluate(policies=[heavy], attrs=ATTRS,
                        namespace_default=Effect.DENY,
                        namespace_max_mode=PolicyMode.ENFORCE)
    baseline = first.model_dump_json(exclude={"latency_us", "evaluated_at",
                                              "decision_id"})
    for _ in range(10_000):
        again = ev.evaluate(policies=[heavy], attrs=ATTRS,
                            namespace_default=Effect.DENY,
                            namespace_max_mode=PolicyMode.ENFORCE)
        assert again.model_dump_json(
            exclude={"latency_us", "evaluated_at", "decision_id"}) == baseline


def test_policy_order_never_changes_the_answer():
    """Deny-overrides has to be order-independent, or two replicas evaluating
    the same request could disagree — and a decision log would record both."""
    import random

    compiled = []
    for i in range(12):
        v = PolicyVersion(policy_id=uuid.uuid4(), version=1,
                          rules=[_busy_rule(i)])
        compiled.append(PolicyCompiler(VELARIS_CATALOG).compile_version(
            policy_id=v.policy_id, tenant_id=None, namespace="ai",
            version=v, mode=PolicyMode.ENFORCE))

    ev = Evaluator()
    baseline = ev.evaluate(policies=compiled, attrs=ATTRS,
                           namespace_default=Effect.DENY,
                           namespace_max_mode=PolicyMode.ENFORCE)
    ids = sorted(m.rule_id for m in baseline.matched)

    for _ in range(200):
        shuffled = compiled[:]
        random.shuffle(shuffled)
        d = ev.evaluate(policies=shuffled, attrs=ATTRS,
                        namespace_default=Effect.DENY,
                        namespace_max_mode=PolicyMode.ENFORCE)
        assert d.effect is baseline.effect
        assert sorted(m.rule_id for m in d.matched) == ids


def test_attribute_insertion_order_never_changes_the_answer(heavy):
    """A dict arrives from a PIP in whatever order the resolver built it."""
    import random

    ev = Evaluator()
    baseline = ev.evaluate(policies=[heavy], attrs=ATTRS,
                           namespace_default=Effect.DENY,
                           namespace_max_mode=PolicyMode.ENFORCE)
    for _ in range(200):
        items = list(ATTRS.items())
        random.shuffle(items)
        d = ev.evaluate(policies=[heavy], attrs=dict(items),
                        namespace_default=Effect.DENY,
                        namespace_max_mode=PolicyMode.ENFORCE)
        assert d.effect is baseline.effect
        assert [m.rule_id for m in d.matched] == [m.rule_id for m in baseline.matched]


def test_the_signature_is_stable_across_ten_thousand_rebuilds():
    """The signature is what proves a version was the one that ran."""
    rules = [_busy_rule(i) for i in range(20)]
    pid = uuid.uuid4()
    first = PolicyVersion(policy_id=pid, version=1, rules=rules).signature()
    for _ in range(10_000):
        assert PolicyVersion(
            policy_id=pid, version=1,
            rules=[Rule.model_validate(r.model_dump(mode="json", by_alias=True))
                   for r in rules]).signature() == first
