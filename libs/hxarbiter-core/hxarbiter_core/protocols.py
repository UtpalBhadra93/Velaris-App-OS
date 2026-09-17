"""HxArbiter — the PIP interface, which is the real integration contract.

A host platform supplies three things and nothing else: resolve identity, fetch
resource attributes, answer region and consent facts. Everything else belongs to
the engine. That is what makes the evaluator portable (§18.3) — not the HTTP
surface, which is only one way to reach it.

Resolution is **lazy and batched**: the compiler records which attribute paths a
policy set actually reads, and only those are resolved, in parallel, under a
budget. A resolver that answers a path it was not asked for is not wrong, but a
resolver that blocks is: the budget exists because a PIP is the one part of a
decision that can touch a network.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .schema import DecisionRequest


@runtime_checkable
class AttributeResolver(Protocol):
    """Resolves one prefix of the attribute catalog."""

    #: The dotted prefix this resolver owns — "tenant", "consent", "resource".
    prefix: str

    async def resolve(
        self, req: DecisionRequest, paths: set[str]
    ) -> dict[str, Any]:
        """Return a map of path → value for the paths asked for.

        Omitting a path is legitimate and means "could not resolve". It must
        never be reported as a value: the evaluator treats an omitted path as
        making every comparison false, so an unresolvable attribute can never
        satisfy an allow rule by accident.

        Raising is also legitimate — the PDP catches it, records the namespace
        as unresolved, and lets the (deny) default apply. What a resolver must
        not do is guess.
        """
        ...


class StaticResolver:
    """A resolver over a fixed map. Used by `/simulate` and by tests.

    It exists so a what-if question is answered by the same code path as a real
    decision. A simulator with its own evaluation logic proves nothing about
    the engine that will actually run.
    """

    def __init__(self, prefix: str, values: dict[str, Any]) -> None:
        self.prefix = prefix
        self._values = values

    async def resolve(
        self, req: DecisionRequest, paths: set[str]
    ) -> dict[str, Any]:
        return {p: self._values[p] for p in paths if p in self._values}


class RequestResolver:
    """Serves `subject.*`, `resource.*` and `context.*` from the request itself.

    These arrive with the caller and need no lookup, but they are still resolved
    through this interface rather than read directly, so that every attribute in
    a decision has come from exactly one place. A mix of "some read here, some
    resolved there" is how an attribute ends up meaning two things.

    **Values here are PEP-supplied, never end-user-supplied** (§14). A PEP is
    trusted code; a request body is not.
    """

    prefix = "*"

    async def resolve(
        self, req: DecisionRequest, paths: set[str]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path in paths:
            head, _, tail = path.partition(".")
            if not tail:
                continue
            if head == "subject":
                if tail == "kind":
                    out[path] = str(req.subject.kind)
                elif tail == "id":
                    out[path] = req.subject.id
                elif tail in req.subject.attrs:
                    out[path] = req.subject.attrs[tail]
            elif head == "resource":
                if tail == "kind":
                    out[path] = req.resource.kind
                elif tail == "id" and req.resource.id is not None:
                    out[path] = req.resource.id
                elif tail in req.resource.attrs:
                    out[path] = req.resource.attrs[tail]
            elif head == "context" and tail in req.context:
                out[path] = req.context[tail]
        return out
