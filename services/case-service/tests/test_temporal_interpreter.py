"""The `definition_json` interpreter (Tier-1 §12 / Tier-2 #21, Option A).

`docs/Future/temporal-decision-record.md` binds Velaris to interpreting the
existing definition at runtime and **forbids** Option B — compiling case types
into workflow code — permanently. The tests that matter here are the ones that
would catch a slide toward Option B or toward a second, divergent copy of the
process semantics:

* the interpreter must delegate advancement to the same function the
  synchronous API path uses, not re-derive it;
* it must go through the lifecycle chokepoint to enter a stage, so assignments,
  SLAs, audit and events all still happen;
* it must carry only ids across the Temporal boundary (constraint 3);
* and it must stay inert for case types that never asked to be interpreted.

The workflow body needs a Temporal server, so it is exercised through its
activities and its registration. The activities are where every decision is
actually made — the workflow only sequences them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import uuid

import pytest

from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
from case_service.temporal.activities import interpreter_activities as ia

pytestmark = pytest.mark.asyncio


def _definition(interpreted=True, stages=None):
    d = {
        "stages": stages if stages is not None else [
            {"id": "review", "name": "Review", "order": 2, "steps": []},
            {"id": "intake", "name": "Intake", "order": 1, "steps": []},
        ]
    }
    if interpreted:
        d["orchestration"] = "temporal"
    return d


async def _case(session, definition, *, status="open", stage=None):
    ct = CaseTypeModel(name=f"IT {uuid.uuid4().hex[:6]}", version="1.0.0",
                       definition_json=definition)
    session.add(ct)
    await session.flush()
    c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                          status=status, current_stage_id=stage)
    session.add(c)
    await session.flush()
    await session.commit()
    return c


class TestPlanOrdering:
    async def test_stages_are_ordered_by_declared_order_not_list_position(self):
        """Must match `_auto_advance_if_complete` exactly, or the two diverge."""
        ids = ia._ordered_stage_ids(_definition())
        assert ids == ["intake", "review"]

    async def test_missing_order_defaults_to_zero(self):
        ids = ia._ordered_stage_ids({"stages": [
            {"id": "b", "order": 1}, {"id": "a"},
        ]})
        assert ids == ["a", "b"]

    async def test_stages_without_ids_are_skipped(self):
        assert ia._ordered_stage_ids({"stages": [{"name": "nameless"}]}) == []

    async def test_empty_definition_is_safe(self):
        assert ia._ordered_stage_ids({}) == []
        assert ia._ordered_stage_ids(None) == []


class TestLoadCasePlan:
    async def test_reports_plan_for_an_interpreted_case_type(self, session):
        c = await _case(session, _definition())
        plan = await ia.load_case_plan(str(c.id))
        assert plan["exists"] is True
        assert plan["interpreted"] is True
        assert plan["stage_ids"] == ["intake", "review"]
        assert plan["terminal"] is False

    async def test_case_type_without_opt_in_is_not_interpreted(self, session):
        """Registration is not activation — the interpreter must stay out."""
        c = await _case(session, _definition(interpreted=False))
        assert (await ia.load_case_plan(str(c.id)))["interpreted"] is False

    async def test_terminal_case_is_flagged(self, session):
        c = await _case(session, _definition(), status="resolved")
        plan = await ia.load_case_plan(str(c.id))
        assert plan["terminal"] is True

    async def test_unknown_case_reports_absence_rather_than_raising(self):
        assert (await ia.load_case_plan(str(uuid.uuid4())))["exists"] is False

    async def test_plan_carries_no_case_data(self, session):
        """Constraint 3: ids and status only cross the Temporal boundary."""
        c = await _case(session, _definition())
        plan = await ia.load_case_plan(str(c.id))
        assert set(plan) == {
            "exists", "status", "terminal", "current_stage_id",
            "stage_ids", "interpreted",
        }


class TestEnterStage:
    async def test_enters_through_the_lifecycle_chokepoint(self, session):
        """Bypassing it would advance a case with no assignments or SLAs."""
        src = inspect.getsource(ia.enter_stage)
        assert "case_lifecycle.on_stage_entered" in src
        assert "update_case_instance" not in src

    async def test_entering_sets_the_stage(self, session):
        c = await _case(session, _definition())
        out = await ia.enter_stage(str(c.id), "intake")
        assert out["entered"] is True

        plan = await ia.load_case_plan(str(c.id))
        assert plan["current_stage_id"] == "intake"

    async def test_unknown_stage_is_refused_not_guessed(self, session):
        c = await _case(session, _definition())
        out = await ia.enter_stage(str(c.id), "nonexistent")
        assert out["entered"] is False
        assert "not in definition" in out["reason"]

    async def test_unknown_case_is_refused(self):
        out = await ia.enter_stage(str(uuid.uuid4()), "intake")
        assert out["entered"] is False


class TestAdvanceDelegation:
    async def test_advance_calls_the_same_function_as_the_api_path(self):
        """The heart of Option A: one definition of 'stage complete', not two."""
        src = inspect.getsource(ia.try_advance)
        assert "_auto_advance_if_complete" in src

    async def test_no_current_stage_means_no_advance(self, session):
        c = await _case(session, _definition())
        assert (await ia.try_advance(str(c.id)))["advanced"] is False

    async def test_stage_with_no_required_steps_does_not_advance(self, session):
        """Mirrors the API guard against infinite auto-advance loops."""
        c = await _case(session, _definition(), stage="intake")
        assert (await ia.try_advance(str(c.id)))["advanced"] is False

    async def test_unknown_case_does_not_advance(self):
        assert (await ia.try_advance(str(uuid.uuid4())))["advanced"] is False


class TestCaseStatus:
    async def test_reports_terminal_for_closed_cases(self, session):
        for status in ia.TERMINAL_STATUSES:
            c = await _case(session, _definition(), status=status)
            assert (await ia.case_status(str(c.id)))["terminal"] is True

    async def test_open_case_is_not_terminal(self, session):
        c = await _case(session, _definition())
        assert (await ia.case_status(str(c.id)))["terminal"] is False

    async def test_missing_case_is_treated_as_terminal(self):
        """A deleted case must stop the interpreter, not spin it forever."""
        out = await ia.case_status(str(uuid.uuid4()))
        assert out["exists"] is False and out["terminal"] is True


class TestRegistrationAndPosture:
    async def test_workflow_and_activities_are_registered(self):
        from case_service.temporal import worker

        assert "CaseInterpreterWorkflow" in [w.__name__ for w in worker.CASE_WORKFLOWS]
        names = {a.__name__ for a in worker.CASE_ACTIVITIES}
        assert {"load_case_plan", "enter_stage", "try_advance", "case_status"} <= names

    async def test_disabled_by_default(self):
        from velaris_shared.config import get_settings

        assert get_settings().temporal_interpreter_enabled is False

    async def test_option_b_is_not_implemented(self):
        """No code generation or compilation of case types anywhere."""
        from case_service.temporal.workflows import case_interpreter_workflow as w

        src = inspect.getsource(w)
        for forbidden in ("exec(", "compile(", "eval(", "generate_workflow"):
            assert forbidden not in src, forbidden

    async def test_workflow_state_holds_only_the_case_id(self):
        """Constraint 3, asserted on the workflow's own fields."""
        from case_service.temporal.workflows.case_interpreter_workflow import (
            CaseInterpreterWorkflow,
        )

        wf = CaseInterpreterWorkflow()
        assert wf._case_id == ""
        # Only scalars/ids — no dicts of case data held between activities.
        for value in vars(wf).values():
            assert isinstance(value, (str, bool, int, type(None)))


class TestStarterWiring:
    """The gap that made this item 'built but unreachable'.

    The workflow was registered with the worker but nothing ever called
    `start_workflow`, so the platform flag and the per-case-type opt-in were
    both inert. These pin the wiring, not the workflow.
    """

    async def _fake_temporal(self, app):
        started: list[dict] = []

        class _Handle:
            async def signal(self, *a, **kw):
                return None

        class _Client:
            async def start_workflow(self, run, arg, *, id, task_queue, **kw):
                started.append({"id": id, "arg": arg})
                return _Handle()

            def get_workflow_handle(self, wid):
                return _Handle()

        app.state.temporal_client = _Client()
        return started

    async def test_interpreter_starts_when_flag_and_opt_in_are_both_on(
        self, client, session, monkeypatch,
    ):
        from velaris_shared.config import get_settings
        from case_service.main import app
        from tests.conftest import deploy_case_type

        started = await self._fake_temporal(app)
        monkeypatch.setattr(get_settings(), "temporal_interpreter_enabled",
                            True, raising=False)
        ct = await deploy_case_type(
            client, name=f"Interp {uuid.uuid4().hex[:6]}",
            definition_json={"stages": [], "orchestration": "temporal"},
        )
        resp = await client.post("/api/v1/cases",
                                 json={"case_type_id": ct["id"], "data": {}})
        assert resp.status_code == 201, resp.text

        ids = [s["id"] for s in started]
        assert any(i.startswith("helix-interpreter-") for i in ids), ids
        assert any(i.startswith("helix-case-") for i in ids), ids
        app.state.temporal_client = None

    async def test_interpreter_does_not_start_without_the_case_type_opt_in(
        self, client, session, monkeypatch,
    ):
        """Flag on is not enough — the case type must ask for it."""
        from velaris_shared.config import get_settings
        from case_service.main import app
        from tests.conftest import deploy_case_type

        started = await self._fake_temporal(app)
        monkeypatch.setattr(get_settings(), "temporal_interpreter_enabled",
                            True, raising=False)
        ct = await deploy_case_type(
            client, name=f"Plain {uuid.uuid4().hex[:6]}",
            definition_json={"stages": []},
        )
        resp = await client.post("/api/v1/cases",
                                 json={"case_type_id": ct["id"], "data": {}})
        assert resp.status_code == 201, resp.text

        assert not any(s["id"].startswith("helix-interpreter-") for s in started)
        app.state.temporal_client = None

    async def test_interpreter_does_not_start_when_the_flag_is_off(
        self, client, session,
    ):
        """Default posture: opted-in case type, flag off, nothing starts."""
        from case_service.main import app
        from tests.conftest import deploy_case_type

        started = await self._fake_temporal(app)
        ct = await deploy_case_type(
            client, name=f"OptIn {uuid.uuid4().hex[:6]}",
            definition_json={"stages": [], "orchestration": "temporal"},
        )
        resp = await client.post("/api/v1/cases",
                                 json={"case_type_id": ct["id"], "data": {}})
        assert resp.status_code == 201, resp.text

        assert not any(s["id"].startswith("helix-interpreter-") for s in started)
        app.state.temporal_client = None

    async def test_signals_fan_out_to_both_workflows(self):
        """Six signal sites and growing — a new one must not miss the
        interpreter, so the fan-out lives in the helper, not the call sites."""
        import inspect

        from case_service.api.routers import cases as cases_router

        src = inspect.getsource(cases_router._signal_lifecycle)
        assert "helix-case-" in src and "helix-interpreter-" in src
