"""F-02 — explainable cost per decision.

"Explainable" is the whole requirement, so the tests are about arithmetic being
checkable rather than about totals being produced:

* every line must satisfy `quantity × unit_rate == amount`, exactly — a float
  here fails on the sixth decimal, which is precisely the check the feature
  exists to survive;
* a case total must **reconcile to the sum of its parts** (the design's own
  exit criterion), across both the stored AI half and the derived labour half;
* an **unpriced** model must read as unpriced and never as free — the failure
  mode where a cost report becomes a lie an operator acts on;
* an **unmeasured** call must produce no line at all, rather than a zero one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from velaris_shared.db.models import (
    ArbiterNamespaceModel,
    DecisionCostModel,
    ModelRateCardModel,
    RateCardModel,
    TenantModel,
)

from case_service.costing import decisions as dcost
from case_service.ledger import records

pytestmark = pytest.mark.asyncio

PREFIX = "/api/v1/costing"

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    _identity.clear_cache()


async def _record(session, tenant, model_id="ollama/llama3.2", case_id=None):
    row = await records.record(
        session, tenant_id=tenant.id, purpose="case_qa", model_id=model_id,
        case_id=case_id, input_text="q", output_text="a")
    await session.commit()
    return row


# ══ rate cards ══════════════════════════════════════════════════════════════


async def test_a_tenant_rate_beats_the_platform_default(session, tenant):
    await dcost.upsert_rate(session, tenant_id=None, model_id="m",
                            input_per_mtok=1, output_per_mtok=2)
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="m",
                            input_per_mtok=10, output_per_mtok=20)
    await session.commit()

    row, source = await dcost.resolve_rate(session, tenant_id=tenant.id, model_id="m")
    assert source == "tenant"
    assert float(row.input_per_mtok) == 10


async def test_a_tenant_with_no_rate_inherits_the_platform_one(session, tenant):
    """And the SOURCE says so: a tenant priced from the platform default is
    paying a rate nobody negotiated for them."""
    await dcost.upsert_rate(session, tenant_id=None, model_id="m",
                            input_per_mtok=1, output_per_mtok=2)
    await session.commit()
    row, source = await dcost.resolve_rate(session, tenant_id=tenant.id, model_id="m")
    assert source == "platform"
    assert float(row.input_per_mtok) == 1


async def test_platform_rows_are_actually_unique(session, tenant):
    """`UNIQUE (tenant_id, model_id)` would NOT constrain them, because
    NULL <> NULL — the mig 177 defect, in the scope where a duplicate does the
    most damage since every tenant inherits it."""
    await dcost.upsert_rate(session, tenant_id=None, model_id="m",
                            input_per_mtok=1, output_per_mtok=2)
    await dcost.upsert_rate(session, tenant_id=None, model_id="m",
                            input_per_mtok=5, output_per_mtok=6)
    await session.commit()

    rows = (await session.execute(
        select(ModelRateCardModel).where(ModelRateCardModel.model_id == "m")
    )).scalars().all()
    assert len(rows) == 1, "a second platform rate was created"
    assert float(rows[0].input_per_mtok) == 5      # upserted, not duplicated
    assert rows[0].tenant_key == "platform"


async def test_a_negative_rate_is_refused(session, tenant):
    with pytest.raises(dcost.CostingError):
        await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="m",
                                input_per_mtok=-1, output_per_mtok=0)


async def test_listing_shows_one_row_per_model(session, tenant):
    """The tenant's own row wins; the inherited one is not listed twice."""
    await dcost.upsert_rate(session, tenant_id=None, model_id="m",
                            input_per_mtok=1, output_per_mtok=2)
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="m",
                            input_per_mtok=10, output_per_mtok=20)
    await dcost.upsert_rate(session, tenant_id=None, model_id="other",
                            input_per_mtok=3, output_per_mtok=4)
    await session.commit()

    rates = await dcost.list_rates(session, tenant_id=tenant.id)
    by_model = {r["model_id"]: r for r in rates}
    assert len(rates) == 2
    assert by_model["m"]["source"] == "tenant"
    assert by_model["other"]["inherited"] is True


# ══ pricing ═════════════════════════════════════════════════════════════════


async def test_one_decision_produces_two_lines(session, tenant):
    """Input and output are separate lines because the actionable half is
    usually OUTPUT — the one a shorter prompt does not fix."""
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=10, output_per_mtok=30)
    await session.commit()
    rec = await _record(session, tenant)

    lines = await dcost.price_ai_decision(
        session, rec, input_tokens=1_000_000, output_tokens=500_000)
    await session.commit()

    assert [l.unit for l in lines] == ["mtok_in", "mtok_out"]
    assert float(lines[0].amount) == 10.0        # 1.0 Mtok × 10
    assert float(lines[1].amount) == 15.0        # 0.5 Mtok × 30


async def test_every_line_reconciles_exactly(session, tenant):
    """quantity × unit_rate == amount, to the stored scale. A float here fails
    on the sixth decimal, which is the check the feature exists to survive."""
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=0.123456, output_per_mtok=7.654321)
    await session.commit()
    rec = await _record(session, tenant)
    lines = await dcost.price_ai_decision(
        session, rec, input_tokens=333_333, output_tokens=777_777)
    await session.commit()

    for line in lines:
        expected = (Decimal(str(line.quantity)) * Decimal(str(line.unit_rate))
                    ).quantize(Decimal("0.000001"))
        assert Decimal(str(line.amount)) == expected, line.unit


async def test_an_unknown_model_is_unpriced_not_free(session, tenant):
    """The failure mode that makes a cost report a lie an operator acts on."""
    rec = await _record(session, tenant, model_id="anthropic/未知")
    lines = await dcost.price_ai_decision(
        session, rec, input_tokens=1_000, output_tokens=1_000)
    await session.commit()

    assert len(lines) == 2
    assert all(l.rate_source == "unpriced" for l in lines)
    assert all(float(l.amount) == 0.0 for l in lines)
    # And the quantity is still recorded — the tokens were spent whether or not
    # we can price them.
    assert float(lines[0].quantity) > 0


async def test_an_unmeasured_call_produces_no_line_at_all(session, tenant):
    """`usage=None` means nobody measured it. A zero line would say the
    decision cost nothing, which is a different and false claim."""
    rec = await _record(session, tenant)
    lines = await dcost.price_from_usage(session, rec, None)
    await session.commit()
    assert lines == []
    assert (await session.execute(
        select(DecisionCostModel))).scalars().first() is None


async def test_pricing_from_chokepoint_usage(session, tenant):
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=2, output_per_mtok=4)
    await session.commit()
    rec = await _record(session, tenant)

    usage = {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
             "tokens_estimated": True}
    lines = await dcost.price_from_usage(session, rec, usage)
    await session.commit()
    assert float(lines[0].amount) == 4.0
    assert float(lines[1].amount) == 4.0
    assert all(l.estimated for l in lines)


async def test_changing_a_rate_does_not_reprice_the_past(session, tenant):
    """Last month's invoice must not move when this month's contract does."""
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=10, output_per_mtok=10)
    await session.commit()
    rec = await _record(session, tenant)
    await dcost.price_ai_decision(session, rec, input_tokens=1_000_000,
                                  output_tokens=0)
    await session.commit()

    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=999, output_per_mtok=999)
    await session.commit()

    line = (await session.execute(
        select(DecisionCostModel).where(DecisionCostModel.unit == "mtok_in")
    )).scalars().first()
    assert float(line.amount) == 10.0


# ══ reconciliation ══════════════════════════════════════════════════════════


async def test_the_case_total_is_the_sum_of_its_parts(session, tenant):
    """The design's own exit criterion, across both halves."""
    case_id = uuid.uuid4()
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=10, output_per_mtok=20)
    session.add(RateCardModel(id=uuid.uuid4(), tenant_id="default", role="*",
                              hourly_rate=60.0, currency="USD"))
    await session.commit()

    for _ in range(3):
        rec = await _record(session, tenant, case_id=case_id)
        await dcost.price_ai_decision(session, rec, input_tokens=100_000,
                                      output_tokens=50_000)
    await session.commit()

    out = await dcost.case_breakdown(
        session, tenant_id=tenant.id, case_id=case_id, tenant_slug="default")
    assert len(out["lines"]) == 6                     # 3 decisions × 2 units
    assert out["labour_included"] is True
    assert round(sum(l["amount"] for l in out["lines"]), 6) == out["total"]
    assert round(sum(out["by_kind"].values()), 6) == out["total"]
    # 3 × (0.1 × 10 + 0.05 × 20) = 3 × 2.0
    assert out["by_kind"]["ai"] == pytest.approx(6.0)


async def test_the_breakdown_says_when_labour_is_excluded(session, tenant):
    """A total that omits the human half is a different number from a total
    that includes it and found none."""
    case_id = uuid.uuid4()
    rec = await _record(session, tenant, case_id=case_id)
    await dcost.price_ai_decision(session, rec, input_tokens=1, output_tokens=1)
    await session.commit()

    out = await dcost.case_breakdown(session, tenant_id=tenant.id, case_id=case_id)
    assert out["labour_included"] is False


async def test_the_breakdown_names_unpriced_models(session, tenant):
    """A total that excludes three unpriced models is not the cost of the case,
    and the reader has to be told which they are."""
    case_id = uuid.uuid4()
    rec = await _record(session, tenant, model_id="openai/gpt-4o", case_id=case_id)
    await dcost.price_ai_decision(session, rec, input_tokens=10, output_tokens=10)
    await session.commit()

    out = await dcost.case_breakdown(session, tenant_id=tenant.id, case_id=case_id)
    assert out["unpriced_lines"] == 2
    assert out["unpriced_models"] == ["openai/gpt-4o"]


async def test_mixed_currencies_are_reported_not_summed_silently(session, tenant):
    """Summing two currencies into one number is the arithmetic error that
    makes a cost report worthless, and it is silent unless somebody checks."""
    case_id = uuid.uuid4()
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="a",
                            input_per_mtok=1, output_per_mtok=1, currency="USD")
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="b",
                            input_per_mtok=1, output_per_mtok=1, currency="EUR")
    await session.commit()
    for model in ("a", "b"):
        rec = await _record(session, tenant, model_id=model, case_id=case_id)
        await dcost.price_ai_decision(session, rec, input_tokens=1, output_tokens=1)
    await session.commit()

    out = await dcost.case_breakdown(session, tenant_id=tenant.id, case_id=case_id)
    assert out["mixed_currency"] is True
    assert out["currency"] is None


async def test_the_rollup_groups_by_model(session, tenant):
    await dcost.upsert_rate(session, tenant_id=tenant.id, model_id="a",
                            input_per_mtok=10, output_per_mtok=10)
    await session.commit()
    for model in ("a", "a", "b"):
        rec = await _record(session, tenant, model_id=model)
        await dcost.price_ai_decision(session, rec, input_tokens=1_000_000,
                                      output_tokens=0)
    await session.commit()

    out = await dcost.tenant_rollup(session, tenant_id=tenant.id)
    assert out["by_model"]["a"]["amount"] == pytest.approx(20.0)
    assert out["by_model"]["b"]["rate_source"] == "unpriced"
    assert out["lines"] == 6


async def _fks_enforced(session) -> bool:
    """SQLite ignores foreign keys unless `PRAGMA foreign_keys=ON`, and this
    harness does not set it. Asserting a cascade here would pass vacuously on
    Postgres and fail on SQLite for a reason that has nothing to do with the
    schema — so the check is made conditional and the real proof is run against
    MySQL, MariaDB and the live PostgreSQL, where the constraint exists."""
    if session.bind.dialect.name != "sqlite":
        return True
    row = (await session.execute(text_("PRAGMA foreign_keys"))).scalar()
    return bool(row)


def text_(sql):
    from sqlalchemy import text
    return text(sql)


async def test_costs_die_with_their_ledger_record(session, tenant):
    """The ledger cascades from the tenant; the costs cascade from the ledger.
    A cost row outliving the decision it prices would be unexplainable, which
    is the one thing this table must not be."""
    if not await _fks_enforced(session):
        pytest.skip("SQLite here does not enforce foreign keys; the cascade is "
                    "proven on MySQL, MariaDB and PostgreSQL instead")
    rec = await _record(session, tenant)
    await dcost.price_ai_decision(session, rec, input_tokens=1, output_tokens=1)
    await session.commit()

    from velaris_shared.db.models import AIDecisionRecordModel
    await session.execute(
        AIDecisionRecordModel.__table__.delete()
        .where(AIDecisionRecordModel.id == rec.id))
    await session.commit()
    assert (await session.execute(
        select(DecisionCostModel))).scalars().first() is None


# ══ the chokepoint ══════════════════════════════════════════════════════════


async def test_the_backend_stashes_what_it_measured():
    """F-02's whole input. Without it a caller has to re-derive token counts
    from the text it holds — a second, quietly different number."""
    from velaris_shared.llm.factory import GuardedBackend

    class _Inner:
        backend_name = "stub"
        _model = "stub-model"
        available = True

        async def complete(self, prompt, **kw):
            return "an answer"

    b = GuardedBackend(_Inner())
    await b.complete("a question", system="")
    usage = b.last_usage
    assert usage is not None
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["tokens_estimated"] is True


async def test_a_failed_call_does_not_keep_the_previous_usage():
    """Leaving the last call's numbers in place would let a failed completion
    be priced as the successful one before it."""
    from velaris_shared.llm.factory import GuardedBackend

    class _Inner:
        backend_name = "stub"
        _model = "stub-model"
        available = True
        fail = False

        async def complete(self, prompt, **kw):
            if self.fail:
                raise RuntimeError("model down")
            return "an answer"

    inner = _Inner()
    b = GuardedBackend(inner)
    await b.complete("a question")
    good = b.last_usage
    inner.fail = True
    with pytest.raises(RuntimeError):
        await b.complete("another question")
    assert b.last_usage is not good or b.last_usage["completion_tokens"] == 0


# ══ the API ═════════════════════════════════════════════════════════════════


def _headers(roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="cost-user", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


async def test_the_new_routes_refuse_an_anonymous_caller(anon_client, tenant):
    for path in ("/model-rates", "/rollup", f"/cases/{uuid.uuid4()}/cost"):
        r = await anon_client.get(f"{PREFIX}{path}")
        assert r.status_code in (401, 403), f"{path} -> {r.status_code}"
    r = await anon_client.put(f"{PREFIX}/model-rates", json={})
    assert r.status_code in (401, 403, 422)


async def test_a_case_worker_cannot_read_costs(client, tenant):
    r = await client.get(f"{PREFIX}/rollup", headers=_headers(["case_worker"]))
    assert r.status_code == 403


async def test_a_tenant_admin_cannot_set_a_platform_rate(client, tenant):
    """A platform row is inherited by every tenant on the box; on a shared
    install "admin" is a TENANT admin."""
    r = await client.put(f"{PREFIX}/model-rates", headers=_headers(["admin"]),
                         json={"model_id": "m", "input_per_mtok": 1,
                               "output_per_mtok": 1, "platform": True})
    assert r.status_code == 403
    assert "PLATFORM-SCOPE" in r.json()["detail"]


async def test_an_admin_sets_and_reads_a_tenant_rate(client, tenant):
    h = _headers(["admin"])
    r = await client.put(f"{PREFIX}/model-rates", headers=h,
                         json={"model_id": "ollama/llama3.2",
                               "input_per_mtok": 1.5, "output_per_mtok": 3.0})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "tenant"

    r = await client.get(f"{PREFIX}/model-rates", headers=h)
    assert r.status_code == 200
    assert r.json()["rates"][0]["input_per_mtok"] == 1.5


async def test_the_case_cost_route_returns_reconciling_lines(
        client, session, tenant):
    case_id = uuid.uuid4()
    await dcost.upsert_rate(session, tenant_id=tenant.id,
                            model_id="ollama/llama3.2",
                            input_per_mtok=10, output_per_mtok=20)
    await session.commit()
    rec = await _record(session, tenant, case_id=case_id)
    await dcost.price_ai_decision(session, rec, input_tokens=100_000,
                                  output_tokens=100_000)
    await session.commit()

    r = await client.get(f"{PREFIX}/cases/{case_id}/cost", headers=_headers(["admin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert round(sum(l["amount"] for l in body["lines"]), 6) == body["total"]
    assert body["total"] == pytest.approx(3.0)      # 0.1×10 + 0.1×20


async def test_unknown_fields_on_a_rate_are_refused(client, tenant):
    r = await client.put(f"{PREFIX}/model-rates", headers=_headers(["admin"]),
                         json={"model_id": "m", "input_per_mtok": 1,
                               "output_per_mtok": 1, "curency": "USD"})
    assert r.status_code == 422
