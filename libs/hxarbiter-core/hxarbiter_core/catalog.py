"""HxArbiter — the registered attribute catalog.

A condition may only name an attribute that appears here. That single rule does
three jobs at once (§4.4, §6):

* A typo becomes a **compile error** instead of a rule that silently never
  matches. A policy that quietly evaluates to nothing is worse than one that
  refuses to activate, because nobody finds out until an auditor asks.
* NL authoring is bounded: the catalog is what the model is shown, so it can
  only reference attributes that exist (§7).
* The compiler knows each path's **type**, which is what lets it reject
  `contains` on a hostname and ordering operators on a set.

The catalog is **data, not code**: a hardcoded one is the thing that would stop
this package being usable by any platform but the one it was written for. No
default catalog ships here — a host registers what its own resolvers can supply.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class AttrType(StrEnum):
    STRING = "string"
    #: A hostname or domain. Comparisons are restricted to `matches_domain`,
    #: `eq`, `ne`, `is_present` and `is_absent` — never `contains`, because
    #: "evil-example.com" contains "example.com".
    DOMAIN = "domain"
    NUMBER = "number"
    BOOLEAN = "boolean"
    STRING_SET = "string_set"


class AttrSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    type: AttrType
    description: str = ""
    #: Closed value set, where one exists. A literal outside it is a compile
    #: error, which catches `region == "EU-WEST"` against a catalog that says
    #: `eu-west` before the policy can ever be activated.
    allowed: tuple[str, ...] | None = None


class AttributeCatalog:
    """An immutable, ordered registry of attribute paths."""

    def __init__(self, specs: list[AttrSpec] | None = None) -> None:
        self._specs: dict[str, AttrSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: AttrSpec) -> None:
        if spec.path in self._specs:
            raise ValueError(f"attribute already registered: {spec.path}")
        self._specs[spec.path] = spec

    def get(self, path: str) -> AttrSpec | None:
        return self._specs.get(path)

    def paths(self) -> list[str]:
        return sorted(self._specs)

    def as_list(self) -> list[AttrSpec]:
        return [self._specs[p] for p in sorted(self._specs)]

    def prefixes(self) -> set[str]:
        return {p.split(".", 1)[0] for p in self._specs}


# NOTE — no default catalog ships with this package, deliberately.
#
# The catalog is the integration contract: a host platform names the attributes
# its own PIPs can actually resolve. A catalog bundled here would promise paths
# nothing supplies, and a rule written against one of those compiles, never
# matches, and looks active forever. Velaris' own registrations live in
# `case_service/hxarbiter/velaris_catalog.py`.
