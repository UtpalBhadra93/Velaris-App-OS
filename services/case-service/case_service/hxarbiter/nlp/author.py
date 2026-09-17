"""HxArbiter ARB-F — natural language to a draft policy.

§7.1 P3: generation is **schema-constrained**. The model is given the rule
schema, the live attribute catalog and the operator semantics, and its output is
parsed by Pydantic and compiled. A validation or compile error is returned to it
**once** for repair, then surfaced to the human.

*No silent fixing.* If the second attempt also fails, the operator sees the
errors and the model's output — not a rule this module invented to make the
request succeed. A policy engine that quietly corrects a compliance rule into
something that compiles is worse than one that refuses.

The prompt contains the catalog and the operator's own words. **It never
contains tenant data** (§19.5) — no case rows, no decision subjects, nothing
from the log.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from hxarbiter_core import AttributeCatalog
from hxarbiter_core import CompileError, PolicyCompiler
from hxarbiter_core import Op, PolicyVersion, Rule

log = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 1


@dataclass
class DraftResult:
    ok: bool
    rules: list[Rule] = field(default_factory=list)
    raw: str = ""
    errors: list[str] = field(default_factory=list)
    attempts: int = 0


def _catalog_block(catalog: AttributeCatalog) -> str:
    lines = []
    for spec in catalog.as_list():
        allowed = (f"  allowed: {', '.join(spec.allowed)}"
                   if spec.allowed else "")
        lines.append(f"- {spec.path} ({spec.type}){allowed} — {spec.description}")
    return "\n".join(lines)


SYSTEM_PROMPT = """\
You translate a policy intent into JSON rules for a deterministic policy engine.

You do not decide anything. Your output is a DRAFT that a human reviews and a
deterministic evaluator later enforces. Correctness matters more than covering
every case: a rule you are unsure about should be omitted and mentioned in
`notes`, never guessed.

OUTPUT: a single JSON object, no prose, no markdown fence:
{"rules": [ ... ], "notes": "..."}

Each rule:
{"id": "<short stable id>", "description": "<one line>",
 "effect": "allow" | "deny", "condition": <condition>,
 "obligations": [{"kind": "...", "params": {}}]}

A condition is one of:
  {"all": [<condition>, ...]}
  {"any": [<condition>, ...]}
  {"not": <condition>}
  {"attr": "<path>", "op": "<op>", "value": <literal or list>}

OPERATORS (nothing else exists):
%(ops)s

RULES YOU MUST FOLLOW:
- Use ONLY the attribute paths listed below. An unlisted path fails to compile.
- For any host or domain comparison use `matches_domain`. NEVER `contains`:
  "evil-example.com" contains "example.com", which is a security bypass.
- `deny` rules must NOT carry obligations.
- Deny-overrides: any matching deny wins. To express "X is refused except Y",
  write the deny narrowly rather than adding an allow — an allow cannot defeat
  a deny.
- Ordering operators (gt/gte/lt/lte) work only on numeric attributes.
- Prefer few precise rules over many broad ones.

AVAILABLE ATTRIBUTES:
%(catalog)s

TARGET NAMESPACE: %(namespace)s (default effect when nothing matches: %(default)s)
"""


def build_prompt(catalog: AttributeCatalog, namespace: str, default_effect: str) -> str:
    return SYSTEM_PROMPT % {
        "ops": "\n".join(f"  {o}" for o in Op),
        "catalog": _catalog_block(catalog),
        "namespace": namespace,
        "default": default_effect,
    }


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def parse_model_output(text: str) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Pull rules out of the model's reply. Never repairs the CONTENT.

    Stripping a markdown fence is presentation, not correction — models add one
    regardless of instructions. Anything beyond that is refused, because the
    difference between "the model wrote it" and "we wrote it for the model"
    is the whole provenance claim.
    """
    cleaned = _FENCE.sub("", text or "").strip()
    if not cleaned:
        return [], "", ["the model returned nothing"]
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return [], "", [f"output was not valid JSON: {exc}"]
    if not isinstance(payload, dict) or "rules" not in payload:
        return [], "", ["output had no `rules` key"]
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        return [], "", ["`rules` was empty or not a list"]
    return rules, str(payload.get("notes") or ""), []


async def draft_rules(
    backend: Any,
    *,
    intent: str,
    catalog: AttributeCatalog,
    namespace: str,
    default_effect: str,
) -> DraftResult:
    """NL intent → validated, compiling rules — or an honest failure."""
    compiler = PolicyCompiler(catalog)
    system = build_prompt(catalog, namespace, default_effect)
    user = intent.strip()
    errors: list[str] = []
    raw = ""

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            raw = await call_model(
                backend, f"{system}\n\n---\n\nINTENT:\n{user}\n")
        except Exception as exc:                               # noqa: BLE001
            return DraftResult(ok=False, raw="", attempts=attempt + 1,
                               errors=[f"the model call failed: {exc}"])

        payload, _notes, parse_errors = parse_model_output(raw)
        if parse_errors:
            errors = parse_errors
        else:
            try:
                rules = [Rule.model_validate(r) for r in payload]
                version = PolicyVersion(
                    policy_id=__import__("uuid").UUID(int=0), version=1, rules=rules)
                report = compiler.check_version(version)
                if report.ok:
                    return DraftResult(ok=True, rules=rules, raw=raw,
                                       attempts=attempt + 1)
                errors = report.errors
            except Exception as exc:                           # noqa: BLE001
                errors = [str(exc)]

        if attempt < MAX_REPAIR_ATTEMPTS:
            # One repair round, with the exact errors. Then it goes to a human.
            user = (
                f"{intent.strip()}\n\n"
                f"Your previous answer was rejected by the compiler:\n"
                + "\n".join(f"- {e}" for e in errors)
                + "\n\nReturn corrected JSON. Do not explain."
            )

    return DraftResult(ok=False, rules=[], raw=raw, errors=errors,
                       attempts=MAX_REPAIR_ATTEMPTS + 1)


async def call_model(backend: Any, prompt: str) -> str:
    """Send one prompt to whatever backend HxNexus handed us.

    Deliberately takes a FINISHED prompt rather than assembling one: readback
    must be able to call a model without any part of the authoring template —
    including the word "INTENT" — appearing in what it sends. Sharing an
    assembler would make that a matter of care rather than of structure.

    Backends in this platform do not share one interface, so the shapes are
    tried in order rather than assumed. Temperature 0 wherever it is accepted:
    authoring must be reproducible, and a policy that differs between two
    identical requests cannot be reviewed.
    """
    for name in ("generate", "complete", "chat", "ask"):
        fn = getattr(backend, name, None)
        if fn is None:
            continue
        try:
            result = fn(prompt, temperature=0)
        except TypeError:
            result = fn(prompt)
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, str) else str(result)
    raise RuntimeError(
        f"backend {type(backend).__name__} exposes no known generate method")
