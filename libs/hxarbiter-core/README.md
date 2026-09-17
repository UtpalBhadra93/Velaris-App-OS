# hxarbiter-core

A deterministic policy evaluator: a closed condition AST, a compiler that
refuses what it cannot decide, and an evaluator that performs **no I/O**.

Extracted from [Velaris](https://github.com/UtpalBhadra93/velaris)' HxArbiter —
its sovereignty and security policy engine — because the evaluator has nothing
platform-specific in it, and a determinism claim nobody can read is a claim.

```bash
pip install hxarbiter-core
```

## Why no I/O

Every fact a decision needs is resolved *before* `evaluate()` is called, so the
same request against the same policy set produces the same decision, always.
That is what makes a decision log worth keeping: an auditor asking *"show me the
rule, and prove it was active on 3 March"* is asking a question only a
deterministic evaluator can answer.

The package has **one dependency** (`pydantic`). The moment it pulls a database
driver or an HTTP client it stops being embeddable, and a reader can no longer
rule out that it reaches a network.

## The condition language is closed

Every operator the evaluator will ever run is named in `Op`. Anything else fails
compilation. That is what makes machine-generated policy safe — a model cannot
emit an operator the compiler does not know — and what makes conflict analysis
decidable.

`matches_domain` exists because `contains` on a hostname is a bypass class:
`evil-example.com` contains `example.com`. The compiler **refuses** `contains` on
any attribute typed `DOMAIN`, so the safe operator is not merely available, it is
the only one reachable.

## Usage

```python
from hxarbiter_core import (
    AttrSpec, AttrType, AttributeCatalog, Effect, Evaluator,
    PolicyCompiler, PolicyMode, PolicyVersion, Rule,
)

catalog = AttributeCatalog([
    AttrSpec(path="tenant.region", type=AttrType.STRING),
    AttrSpec(path="context.provider_class", type=AttrType.STRING,
             allowed=("local", "external")),
])

rule = Rule.model_validate({
    "id": "no-external-ai-for-regulated-data",
    "effect": "deny",
    "condition": {"all": [
        {"attr": "tenant.region", "op": "eq", "value": "region-a"},
        {"attr": "context.provider_class", "op": "eq", "value": "external"},
    ]},
})

version  = PolicyVersion(policy_id=uuid4(), version=1, rules=[rule])
compiled = PolicyCompiler(catalog).compile_version(
    policy_id=version.policy_id, tenant_id=None, namespace="ai",
    version=version, mode=PolicyMode.ENFORCE)

decision = Evaluator().evaluate(
    policies=[compiled],
    attrs={"tenant.region": "region-a", "context.provider_class": "external"},
    namespace_default=Effect.DENY,
    namespace_max_mode=PolicyMode.ENFORCE,
)
decision.effect      # Effect.DENY
decision.matched     # [MatchedRule(rule_id='no-external-ai-for-regulated-data', ...)]
```

## Semantics worth knowing before you write a policy

- **Deny-overrides.** Any matching `deny` wins, whatever else matched. To express
  "X is refused except Y", narrow the deny — an `allow` cannot defeat a `deny`.
- **A missing attribute makes a comparison false, never true.** An attribute
  nothing could resolve can never satisfy an `allow` by accident; the request
  falls through to the namespace default.
- **Modes clamp downwards.** `clamp(version_mode, namespace_ceiling)` takes the
  weaker of the two, so a whole namespace can be held in `shadow` during
  roll-out regardless of how any individual policy was authored.
- **`PolicyVersion.signature()`** is SHA-256 over canonical JSON — sorted keys,
  no whitespace — so a version signs to the same digest anywhere. A signature
  that depended on dict order could not prove which version ran.
- **A condition's right-hand side can be another attribute**, so a rule need not
  name a jurisdiction at all:

  ```json
  {"attr": "resource.region", "op": "ne", "value": {"attr": "tenant.region"}}
  ```

  reads as *"the data is not where the tenant is"* and works for any region
  scheme you resolve — the engine never interprets the value. Both paths must be
  registered and share a type; the compiler refuses the comparison otherwise
  rather than letting it evaluate to a silent `false` for the life of the policy.

## Obligations — you must honour them or refuse

An `allow` rule may carry **obligations**: *you may export this, if you
watermark it.* `Decision.obligations` hands them back, and the integration
contract is that **you carry them out or you refuse.**

```python
if decision.effect is not Effect.ALLOW:
    return refuse()
for ob in decision.obligations:
    if ob.kind not in WHAT_I_IMPLEMENT:
        return refuse()      # an allow you cannot honour is not an allow
    apply(ob)
```

`ObligationKind` is a closed enum for exactly this reason: an obligation your
code has never heard of should be **unrepresentable in a policy**, not quietly
ignored at your end.

**A `bool` return type is the mistake to avoid.** A helper that collapses a
decision to true/false discards the obligations, and a policy author who writes
"allow, but pseudonymise" gets a plain allow — a rule that reads as a control and
is not one.

## What is not here

The enforcement points, the persistence layer, the tamper-evident decision chain
and the authoring UI are Velaris' and are not Apache-2.0. This package is the
evaluator.

## License

**Business Source License 1.1.** Source-available, not open source.

- **Free for any non-production use**, without limit and without asking —
  evaluation, development, testing, benchmarking, security review, teaching.
  The point is that you can find out whether this fits your problem, on your own
  data, before paying for it.
- **Free in production inside a Velaris deployment.** A Velaris licence covers it.
- **Any other production use needs a commercial licence.**
- **Redistribution needs a separate agreement** — you may not ship it inside your
  own product or offer it as a hosted service.

Each release converts to **Apache-2.0 four years after publication**.

Source-available is deliberate rather than incidental: a determinism claim nobody
can read is a claim, and an auditor has to be able to check the evaluator that
decided against them. Reading it does not require open source; using it in
production does require a licence.

Contact: velaris.app.os@gmail.com
