"""HxSentinel P5 — the local risk-scoring model (numpy logistic regression).

Pins: training refuses on too-little / single-class data; it learns ONLY the
label-independent features (no leakage from outcome-derived ones); it reports a
real holdout AUC; activation is human-gated and archives the prior; and an
active model produces a `model_risk` recommendation finding (pending models do
not infer). All local — numpy, no external service.

Setup inserts cases directly on the session (no client) so training/inference is
deterministic (evaluate_case + build_feature_context only need the rows).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from velaris_shared.db.models import CaseInstanceModel, CaseInstanceVariableModel
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import model as risk_model
from case_service.sentinel import service as sentinel
from tests.phase_hxsentinel._seed import seed_case_type

RISK = "/api/v1/risk"
CT = uuid.uuid4()          # shared case type so amount_zscore has a distribution


@pytest.fixture(autouse=True)
def _enable_hxsentinel():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("hxsentinel")
    _ENABLED_VERSIONS["hxsentinel"] = "v1.9.0"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("hxsentinel", None)
    else:
        _ENABLED_VERSIONS["hxsentinel"] = prior


async def _labelled_case(session, amount, is_fraud, *, tenant=None):
    cid = uuid.uuid4()
    await seed_case_type(session, CT)   # FK parent (real engines enforce case_type_id)
    session.add(CaseInstanceModel(id=cid, case_type_id=CT, case_type_version="1.0.0", tenant_id=tenant))
    session.add(CaseInstanceVariableModel(case_id=cid, full_key="loan.amount",
                value_num=float(amount), written_by="t"))
    await session.commit()
    await sentinel.set_outcome(session, case_id=cid, tenant_id="default",
                               outcome="fraud" if is_fraud else "clean", labelled_by="t", audit=False)
    return cid


# High-amount fraud vs low-amount clean, with a little overlap so AUC < 1.
_FRAUD = [8000, 9000, 10000, 11000, 12000, 7000, 13000, 8500, 9500, 10500, 11500, 6000]
_CLEAN = [100, 200, 300, 400, 500, 150, 250, 350, 450, 550, 1000, 5500]


async def _train_corpus(session):
    for a in _FRAUD:
        await _labelled_case(session, a, True)
    for a in _CLEAN:
        await _labelled_case(session, a, False)
    await session.commit()


class TestTraining:
    async def test_refuses_too_few(self, session, client):
        await _labelled_case(session, 9000, True)
        await _labelled_case(session, 100, False)
        await session.commit()
        with pytest.raises(risk_model.TrainError):
            await risk_model.train_model(session, "default")

    async def test_refuses_single_class(self, session, client):
        for a in _CLEAN + _CLEAN:      # 24 clean, 0 fraud
            await _labelled_case(session, a, False)
        await session.commit()
        with pytest.raises(risk_model.TrainError):
            await risk_model.train_model(session, "default")

    async def test_trains_leakage_free_with_real_auc(self, session, client):
        await _train_corpus(session)
        m = await risk_model.train_model(session, "default")
        # Only label-independent features — NEVER the outcome-derived ones.
        assert set(m.coefficients) == set(risk_model.MODEL_FEATURES)
        assert "shares_doc_with_bad" not in m.coefficients
        assert "shared_signals_with_bad" not in m.coefficients
        # A real, honest signal (amount genuinely separates) — not a leaked 1.0-by-construction.
        assert m.metrics["holdout_auc"] is not None and m.metrics["holdout_auc"] >= 0.7
        assert m.status == "pending" and m.trained_on == 24


class TestActivationAndInference:
    async def _active_model(self, session):
        await _train_corpus(session)
        m = await risk_model.train_model(session, "default")
        await risk_model.activate_model(session, "default", m.version, actor="boss")
        await session.commit()
        return m

    async def test_pending_model_does_not_infer(self, session, client):
        await _train_corpus(session)
        await risk_model.train_model(session, "default")   # pending, NOT activated
        await session.commit()
        ca = await _labelled_case(session, 12000, False)   # high amount
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert not any(f.rule_key == "model_risk" for f in active)

    async def test_active_model_scores_high_amount(self, session, client):
        await self._active_model(session)
        ca = uuid.uuid4()
        await seed_case_type(session, CT)
        session.add(CaseInstanceModel(id=ca, case_type_id=CT, case_type_version="1.0.0"))
        session.add(CaseInstanceVariableModel(case_id=ca, full_key="loan.amount",
                    value_num=15000.0, written_by="t"))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        mf = [f for f in active if f.rule_key == "model_risk"]
        assert mf, "an active model should score a high-amount case"
        assert mf[0].evidence["source"] == "model" and 0 <= mf[0].evidence["score"] <= 1
        assert mf[0].evidence["top_factors"]              # explainable

    async def test_active_model_low_amount_no_finding(self, session, client):
        await self._active_model(session)
        ca = uuid.uuid4()
        await seed_case_type(session, CT)
        session.add(CaseInstanceModel(id=ca, case_type_id=CT, case_type_version="1.0.0"))
        session.add(CaseInstanceVariableModel(case_id=ca, full_key="loan.amount",
                    value_num=50.0, written_by="t"))
        await session.commit()
        active = await risk_engine.evaluate_case(session, ca)
        assert not any(f.rule_key == "model_risk" for f in active)

    async def test_activation_archives_prior(self, session, client):
        await self._active_model(session)               # v1 active
        m2 = await risk_model.train_model(session, "default")
        await risk_model.activate_model(session, "default", m2.version, actor="boss")
        await session.commit()
        models = {m.version: m.status for m in await risk_model.list_models(session, "default")}
        assert models[1] == "archived" and models[2] == "active"


class TestEndpoints:
    async def test_train_endpoint_refuses_then_trains(self, session, client):
        assert (await client.post(f"{RISK}/model/train")).status_code == 400   # no data
        await _train_corpus(session)
        r = await client.post(f"{RISK}/model/train")
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "pending"
        act = await client.post(f"{RISK}/model/{r.json()['version']}/activate")
        assert act.status_code == 200 and act.json()["status"] == "active"

    async def test_activate_unknown_404(self, client):
        assert (await client.post(f"{RISK}/model/99/activate")).status_code == 404
