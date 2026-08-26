from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from case_service.bots import service
from velaris_shared.db.models import BotDefinitionModel, BotJobModel

DIGEST = "sha256:" + "a" * 64

async def _bot(session, **kw):
    row = BotDefinitionModel(
        tenant_id=kw.pop("tenant_id", "default"), name=f"p-{uuid.uuid4().hex[:8]}",
        description="", image="registry.test/bot", digest=DIGEST,
        declared_command=[], declared_env={}, credential_refs=[],
        input_schema={}, output_schema={}, granted_domains=[],
        status="active", created_by="author", **kw)
    session.add(row); await session.commit(); await session.refresh(row)
    return row

@pytest.mark.asyncio
async def test_default_bot_now_runs(session):
    bot = await _bot(session, max_concurrency=1)
    await service.enqueue(session, bot=bot, input_data={}, trigger="api")
    await session.commit()
    job = await service.lease_next(session)
    assert job is not None and job.status == "leased"

@pytest.mark.asyncio
async def test_second_job_waits_for_the_first(session):
    bot = await _bot(session, max_concurrency=1)
    await service.enqueue(session, bot=bot, input_data={}, trigger="api")
    await service.enqueue(session, bot=bot, input_data={}, trigger="api")
    await session.commit()
    first = await service.lease_next(session); await session.commit()
    second = await service.lease_next(session); await session.commit()
    assert first is not None and second is None

@pytest.mark.asyncio
async def test_backoff_is_real(session):
    bot = await _bot(session)
    await service.enqueue(session, bot=bot, input_data={}, trigger="api")
    await session.commit()
    job = await service.lease_next(session); await session.commit()
    await service.requeue_or_fail(session, job, error="boom"); await session.commit()
    assert job.run_after is not None
    again = await service.lease_next(session)
    assert again is None, "a failed job must not come back before its backoff"
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert (await service.lease_next(session, now=later)) is not None

@pytest.mark.asyncio
async def test_tenant_pool_bounds_all_bots_together(session):
    t = f"t-{uuid.uuid4().hex[:6]}"
    bots = [await _bot(session, tenant_id=t, max_concurrency=5) for _ in range(6)]
    for b in bots:
        await service.enqueue(session, bot=b, input_data={}, trigger="api")
    await session.commit()
    leased = 0
    while (j := await service.lease_next(session)) is not None:
        await session.commit(); leased += 1
        if leased > 10: break
    assert leased == 4, f"tenant default is 4, leased {leased}"

@pytest.mark.asyncio
async def test_fanout(session):
    bot = await _bot(session, fanout_input_key="rows", fanout_max=3)
    jobs = await service.enqueue_run(
        session, bot=bot, input_data={"rows": [1, 2, 3], "x": "keep"}, trigger="api")
    await session.commit()
    assert len(jobs) == 3
    assert [j.input["rows"] for j in jobs] == [1, 2, 3]
    assert all(j.input["x"] == "keep" for j in jobs)
    assert len({j.batch_id for j in jobs}) == 1
    s = await service.batch_summary(session, jobs[0].batch_id)
    assert s["size"] == 3 and s["finished"] is False
    with pytest.raises(service.BotError):
        await service.enqueue_run(
            session, bot=bot, input_data={"rows": [1, 2, 3, 4]}, trigger="api")
