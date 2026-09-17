"""The `hxarbiter-core` boundary — one copy of the evaluator, and it stays pure.

Two ways this package quietly stops being publishable, both of which look like
ordinary maintenance at the time:

* **A second copy appears.** Someone adds `case_service/hxarbiter/evaluator.py`
  back "just to tweak something", and from then on the published package and the
  running platform are different programs. The drift is invisible until two
  decisions disagree.
* **A dependency creeps in.** The moment the core imports SQLAlchemy, FastAPI or
  an HTTP client it stops being embeddable — and "the evaluator is
  deterministic" stops being inspectable, because a reader can no longer rule
  out that it reaches a network.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import hxarbiter_core
from case_service import hxarbiter as platform_side

CORE = pathlib.Path(hxarbiter_core.__file__).parent
PLATFORM = pathlib.Path(platform_side.__file__).parent

#: Everything the core owns. None of it may exist a second time in the platform.
CORE_MODULES = {"schema", "catalog", "compiler", "evaluator", "protocols", "analyzer"}

#: What the core may import. stdlib plus exactly one third-party package.
ALLOWED_THIRD_PARTY = {"pydantic"}


def test_the_platform_holds_no_second_copy_of_the_core():
    duplicated = {p.stem for p in PLATFORM.rglob("*.py")} & CORE_MODULES
    assert not duplicated, (
        f"these modules exist in BOTH hxarbiter-core and case_service: "
        f"{sorted(duplicated)}. The published package and the running platform "
        f"would be different programs.")


def test_the_platform_uses_the_package_and_not_a_copy():
    assert platform_side.Evaluator is hxarbiter_core.Evaluator
    assert platform_side.PolicyCompiler is hxarbiter_core.PolicyCompiler
    assert platform_side.Rule is hxarbiter_core.Rule


@pytest.mark.parametrize(
    "module", sorted(p.name for p in CORE.glob("*.py")))
def test_the_core_imports_nothing_but_stdlib_and_pydantic(module):
    tree = ast.parse((CORE / module).read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:                 # relative import, inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])

    import sys
    third_party = {
        r for r in roots
        if r not in sys.stdlib_module_names and r != "hxarbiter_core"
    }
    assert third_party <= ALLOWED_THIRD_PARTY, (
        f"{module} imports {sorted(third_party - ALLOWED_THIRD_PARTY)}. The core "
        f"must stay embeddable and provably I/O-free.")


def test_the_core_ships_no_platform_specific_catalog():
    """A bundled catalog would promise attributes nothing supplies.

    A rule written against such a path compiles, never matches, and looks active
    forever — which is worse than the path not existing.
    """
    assert not hasattr(hxarbiter_core, "VELARIS_CATALOG")
    text = (CORE / "catalog.py").read_text()
    assert "VELARIS_CATALOG" not in text


def test_velaris_registers_its_own_catalog_on_top():
    from case_service.hxarbiter import VELARIS_CATALOG

    assert isinstance(VELARIS_CATALOG, hxarbiter_core.AttributeCatalog)
    assert "tenant.region" in VELARIS_CATALOG.paths()


def test_the_package_declares_only_pydantic():
    pyproject = (CORE.parent / "pyproject.toml").read_text()
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = {line.strip().strip('",').split(">")[0].split("=")[0].strip()
                for line in block.splitlines() if line.strip().strip('",')}
    assert declared == {"pydantic"}, declared
