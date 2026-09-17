"""Document indexing off the request (migration 203).

`index_document` is awaited INSIDE the request and the case-wide route loops
every document, so with OCR on, one slow scan is multiplied by the number of
scans rather than paid once. The queue moves that work to a schedule.

What these tests hold down, in order of how badly each would hurt:

* **the default is still the synchronous path** — an install that takes this
  update and asks for nothing behaves exactly as it did, and writes no queue
  row at all;
* **a queued document is not queued twice** — a double-clicked button must not
  double an OCR bill;
* **a failure is a row, never a lost document** — it goes back to pending while
  attempts remain, and is `failed` with the reason on the row after that;
* **two drains cannot run the same job** — the claim is a conditional UPDATE,
  not a read followed by a write;
* **a tenant's schedule drains that tenant's work** — compared with
  `same_tenant`, because the row holds a GUID and a schedule holds a token that
  is a slug on many installs. Comparing those with `==` is how costing came to
  answer "case not found" to a case's own tenant.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    DocumentChunkModel,
    DocumentIndexJobModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.hxnexus import index_queue
from case_service.main import app
from case_service.scheduler import actions as scheduler_actions
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio


# ── Harness ──────────────────────────────────────────────────────────────────


class _FakeStorage:
    """Storage that answers with what the test put in it, and can be made to
    fail on demand — the failure path is most of what matters here."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.fail_with: Exception | None = None
        self.reads = 0

    async def get(self, key: str) -> bytes:
        self.reads += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.blobs[key]


@pytest.fixture
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("case_service.storage.get_storage_backend", lambda: fake)
    # The synchronous route bound the name at import time, so patching the
    # package alone would leave that path on the real filesystem backend — and
    # its `except Exception: continue` would swallow the miss and report zero
    # documents indexed, which reads as a passing test of nothing.
    monkeypatch.setattr(
        "case_service.api.routers.hxnexus.get_storage_backend", lambda: fake)
    return fake


def _token(username: str, roles=("admin",)) -> str:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")


def _headers(username: str = "ann") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(username)}"}


async def _add_document(db, case_id, storage, *, filename="scan.txt",
                        body=b"the quick brown fox jumps over the lazy dog",
                        content_type="text/plain"):
    doc = DocumentModel(
        id=uuid.uuid4(), case_id=case_id, filename=filename,
        content_type=content_type, current_version=1, uploaded_by="ann",
    )
    db.add(doc)
    await db.flush()
    key = f"docs/{doc.id}/1"
    storage.blobs[key] = body
    db.add(DocumentVersionModel(
        id=uuid.uuid4(), document_id=doc.id, version=1, storage_key=key,
        size_bytes=len(body), sha256="0" * 64, uploaded_by="ann",
    ))
    await db.commit()
    return doc


@pytest_asyncio.fixture
async def seeded(storage):
    """One tenant, one case in it, two documents on the case."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        db.add(acme)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Queue CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ACME-Q1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="medium",
            tenant_id=acme.id, created_by="ann", data={})
        db.add(case)
        await db.commit()
        first = await _add_document(db, case.id, storage)
        second = await _add_document(db, case.id, storage, filename="second.txt")
        return {"tenant": acme, "case": case, "docs": [first, second]}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


async def _jobs(db, **where):
    query = select(DocumentIndexJobModel)
    for key, value in where.items():
        query = query.where(getattr(DocumentIndexJobModel, key) == value)
    return (await db.execute(query.order_by(DocumentIndexJobModel.created_at))).scalars().all()


# ── The default has not moved ────────────────────────────────────────────────


class TestTheDefaultIsUnchanged:
    async def test_indexing_without_asking_stays_synchronous(self, api, seeded, storage):
        """An install that takes this update and changes nothing gets exactly
        what it got before: the chunks exist when the response arrives, and
        there is no queue row anywhere.

        The case here is TENANTED on purpose. Writing this test is what found a
        pre-existing 500 on that path: `cases.tenant_id` is a GUID and
        `hxnexus_document_chunks.tenant_id` is a VARCHAR, so the value could not
        bind. Every existing test indexed an untenanted case, so the whole
        multi-tenant half of this route had never been run.
        """
        case_id = seeded["case"].id
        r = await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                           headers=_headers(), json={})
        assert r.status_code == 200, r.text
        assert r.json()["documents_indexed"] == 2
        assert r.json()["total_chunks"] > 0

        async with TestSessionFactory() as db:
            assert await _jobs(db) == []
            chunks = (await db.execute(select(DocumentChunkModel).where(
                DocumentChunkModel.case_id == case_id))).scalars().all()
            assert chunks

    async def test_an_empty_queue_drains_to_a_stated_nothing(self):
        """"0 indexed" and "nothing was waiting" look identical on a history
        screen, so the run says which one happened."""
        async with TestSessionFactory() as db:
            out = await index_queue.run_due_jobs(db)
        assert out["claimed"] == 0 and out["indexed"] == 0


# ── Enqueue ──────────────────────────────────────────────────────────────────


class TestEnqueue:
    async def test_background_queues_one_job_per_document_and_indexes_nothing_yet(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        r = await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                           headers=_headers(), json={"background": True})
        assert r.status_code == 200, r.text
        assert r.json()["queued"] == 2

        async with TestSessionFactory() as db:
            rows = await _jobs(db)
            assert [row.status for row in rows] == ["pending", "pending"]
            assert {row.document_id for row in rows} == {d.id for d in seeded["docs"]}
            # The tenant travels on the row: the drain has no caller to ask.
            assert all(str(row.tenant_id) == str(seeded["tenant"].id) for row in rows)
            assert all(row.requested_by for row in rows)
            chunks = (await db.execute(select(DocumentChunkModel))).scalars().all()
            assert chunks == []
        assert storage.reads == 0

    async def test_queueing_twice_does_not_queue_twice(self, api, seeded):
        """A double-clicked button must not double an OCR bill."""
        case_id = seeded["case"].id
        url = f"/api/v1/hxnexus/cases/{case_id}/documents/index"
        await api.post(url, headers=_headers(), json={"background": True})
        second = await api.post(url, headers=_headers(), json={"background": True})

        assert second.json()["queued"] == 0
        assert second.json()["already_queued"] == 2
        async with TestSessionFactory() as db:
            assert len(await _jobs(db)) == 2

    async def test_reindex_is_widened_never_narrowed(self, seeded):
        """A second, cheaper request must not silently cancel the first's
        intent to drop the stale chunks."""
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann", reindex=True)
            row, created = await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann", reindex=False)
        assert created is False
        assert row.reindex is True

    async def test_a_finished_document_can_be_queued_again(self, seeded, storage):
        """`active_key` constrains the ACTIVE rows only — re-indexing a document
        later is the normal case, not a duplicate."""
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            await index_queue.run_due_jobs(db)
            _, created = await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann", reindex=True)
        assert created is True


# ── The drain ────────────────────────────────────────────────────────────────


class TestDrain:
    async def test_a_drained_job_indexes_the_document_and_records_the_count(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                       headers=_headers(), json={"background": True})

        async with TestSessionFactory() as db:
            out = await index_queue.run_due_jobs(db)

        assert out["claimed"] == 2 and out["indexed"] == 2
        assert out["chunks"] > 0
        async with TestSessionFactory() as db:
            rows = await _jobs(db)
            assert {row.status for row in rows} == {"done"}
            assert all(row.chunks_indexed and row.chunks_indexed > 0 for row in rows)
            # Freed: a done job holds no claim on its document.
            assert all(row.active_key is None for row in rows)
            assert all(row.finished_at is not None for row in rows)
            chunks = (await db.execute(select(DocumentChunkModel).where(
                DocumentChunkModel.case_id == case_id))).scalars().all()
            assert chunks

    async def test_the_count_bounds_one_drain(self, seeded, storage):
        async with TestSessionFactory() as db:
            for doc in seeded["docs"]:
                await index_queue.enqueue_document(
                    db, document_id=doc.id, case_id=seeded["case"].id,
                    tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db, max_jobs=1)
        assert out["claimed"] == 1
        async with TestSessionFactory() as db:
            assert sorted(r.status for r in await _jobs(db)) == ["done", "pending"]

    async def test_the_wall_clock_bounds_one_drain_and_says_so(self, seeded, storage):
        """A drain that never returns is a stalled schedule, and a schedule that
        stalled silently is the failure this whole queue exists to avoid."""
        async with TestSessionFactory() as db:
            for doc in seeded["docs"]:
                await index_queue.enqueue_document(
                    db, document_id=doc.id, case_id=seeded["case"].id,
                    tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db, budget_seconds=-1)
        assert out["claimed"] == 0
        assert out["stopped_on_budget"] is True

    async def test_reindex_drops_the_previous_chunks(self, seeded, storage):
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            await index_queue.run_due_jobs(db)
            first = len((await db.execute(select(DocumentChunkModel).where(
                DocumentChunkModel.document_id == doc.id))).scalars().all())

            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann", reindex=True)
            await index_queue.run_due_jobs(db)
            after = len((await db.execute(select(DocumentChunkModel).where(
                DocumentChunkModel.document_id == doc.id))).scalars().all())

        assert first > 0
        assert after == first, "reindex must replace the chunks, not duplicate them"


# ── Failure ──────────────────────────────────────────────────────────────────


class TestFailure:
    async def test_a_storage_failure_goes_back_to_pending_with_the_reason(
            self, seeded, storage):
        """A storage blip and an unreadable file raise the same exception here.
        Only one of them is worth another OCR timeout, which is what
        `max_attempts` decides — so the first failure is a retry, not a burial."""
        doc = seeded["docs"][0]
        storage.fail_with = RuntimeError("object store unreachable")
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db)

        assert out["indexed"] == 0 and out["failed"]
        async with TestSessionFactory() as db:
            row = (await _jobs(db))[0]
            assert row.status == "pending"
            assert row.attempts == 1
            assert "object store unreachable" in row.last_error
            # Still claimed: it is still this document's active job.
            assert row.active_key == str(doc.id)

    async def test_it_gives_up_after_max_attempts_and_frees_the_document(
            self, seeded, storage):
        doc = seeded["docs"][0]
        storage.fail_with = RuntimeError("nope")
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            for _ in range(3):
                await index_queue.run_due_jobs(db)
            rows = await _jobs(db)

        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].attempts == 3, "one attempt per drain, not two"
        assert rows[0].active_key is None

    async def test_a_document_deleted_before_the_drain_is_not_retried_forever(
            self, seeded, storage):
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=uuid.uuid4(), case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            await index_queue.run_due_jobs(db)
            row = (await _jobs(db))[0]
        assert row.status == "failed"
        assert "no longer exists" in row.last_error


# ── What may have changed since the job was queued ───────────────────────────
#
# The gap between enqueue and drain is the whole risk of a queue, and these are
# the three facts that can move inside it.


class TestTimeBetweenEnqueueAndDrain:
    async def test_an_erased_case_is_not_indexed_after_the_fact(self, seeded, storage):
        """**A background path must not be able to un-erase a case.**

        Indexing writes the document's text into `hxnexus_document_chunks` — a
        derived copy HxNexus reads back. A job queued before an erasure and
        drained after it would create that copy *after* the case was
        anonymized, which is the one thing erasure exists to prevent.
        """
        from case_service.compliance.retention import ANON_MARKER

        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")

            case = await db.get(CaseInstanceModel, seeded["case"].id)
            case.data = {ANON_MARKER: "2026-09-10T00:00:00+00:00"}
            await db.commit()

            out = await index_queue.run_due_jobs(db)

        assert out["indexed"] == 0
        async with TestSessionFactory() as db:
            row = (await _jobs(db))[0]
            assert row.status == "failed"
            assert "no lawful basis" in row.last_error
            chunks = (await db.execute(select(DocumentChunkModel))).scalars().all()
            assert chunks == [], "an erased case must gain no derived text"
        assert storage.reads == 0, "the document was not even read"

    async def test_a_document_deleted_after_queueing_is_not_indexed(
            self, seeded, storage):
        """Its text would go back into the one table where a deleted document's
        content can still be read out."""
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            row = await db.get(DocumentModel, doc.id)
            row.is_deleted = True
            await db.commit()

            out = await index_queue.run_due_jobs(db)

        assert out["indexed"] == 0
        async with TestSessionFactory() as db:
            job = (await _jobs(db))[0]
            assert job.status == "failed"
            assert "deleted" in job.last_error
            assert (await db.execute(select(DocumentChunkModel))).scalars().all() == []

    async def test_a_case_deleted_after_queueing_is_refused_not_crashed(
            self, seeded, storage):
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=uuid.uuid4(),
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db)

        assert out["indexed"] == 0
        async with TestSessionFactory() as db:
            assert "unknown_case" in (await _jobs(db))[0].last_error


class TestAJobThatWasAbandoned:
    async def test_a_stale_running_job_comes_back(self, seeded, storage):
        """A drain killed mid-document — a restart, an OOM — leaves the row
        `running` with `active_key` still set. Without a reclaim nothing would
        ever pick it up, and because that key is what prevents duplicates, the
        document could never be queued again either: a permanent block produced
        by the safeguard against duplicates.
        """
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            job = (await _jobs(db))[0]
            await index_queue._claim(db, job)
            # Backdate the claim rather than waiting an hour.
            job.started_at = datetime.now(timezone.utc) - timedelta(hours=2)
            await db.commit()

            reclaimed = await index_queue.reclaim_stale(db)
            assert reclaimed == 1

            out = await index_queue.run_due_jobs(db)

        assert out["indexed"] == 1
        async with TestSessionFactory() as db:
            row = (await _jobs(db))[0]
            assert row.status == "done"
            # One attempt per claim: the reclaim must not spend a retry on an
            # outage that had nothing to do with the document.
            assert row.attempts == 2

    async def test_a_job_running_normally_is_left_alone(self, seeded, storage):
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            job = (await _jobs(db))[0]
            await index_queue._claim(db, job)

            assert await index_queue.reclaim_stale(db) == 0


class TestTwoEnqueuesAtOnce:
    async def test_the_loser_of_the_race_reports_already_queued(
            self, seeded, storage, monkeypatch):
        """The unique index decides, not the SELECT — which can be stale by the
        time the INSERT lands. The loser must answer "already queued", not 500.
        """
        doc = seeded["docs"][0]
        from sqlalchemy.exc import IntegrityError

        async with TestSessionFactory() as db:
            # The winner's row, already in the table.
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")

        async def _boom(*a, **kw):
            raise IntegrityError("INSERT", {}, Exception("duplicate active_key"))

        monkeypatch.setattr(index_queue, "_insert", _boom)
        async with TestSessionFactory() as db:
            # Force the SELECT to miss, so the INSERT is the thing that refuses.
            row, created = await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann",
                reindex=False)
        assert created is False
        assert str(row.document_id) == str(doc.id)


# ── Two drains ───────────────────────────────────────────────────────────────


class TestConcurrency:
    async def test_only_one_drain_can_claim_a_job(self, seeded, storage):
        """The claim is a conditional UPDATE. A read followed by an
        unconditional write is exactly how two schedules would both OCR the
        same scan."""
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            job = (await _jobs(db))[0]
            first = await index_queue._claim(db, job)
            second = await index_queue._claim(db, job)

        assert first is True
        assert second is False


# ── Tenancy ──────────────────────────────────────────────────────────────────


class TestTenancy:
    async def test_another_tenants_job_is_not_drained(self, seeded, storage):
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            beta = TenantModel(id=uuid.uuid4(), slug="beta", name="Beta")
            db.add(beta)
            await db.commit()
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db, tenant_id="beta")

        assert out["claimed"] == 0

    async def test_a_schedules_slug_matches_a_rows_guid(self, seeded, storage):
        """The row holds the case's tenant GUID; a schedule holds whatever the
        operator's tenant is called. `==` between those two is the defect that
        made costing answer "case not found" to a case's own tenant."""
        doc = seeded["docs"][0]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            out = await index_queue.run_due_jobs(db, tenant_id="acme")

        assert out["claimed"] == 1 and out["indexed"] == 1

    async def test_an_untenanted_job_is_drained_by_a_tenants_schedule(
            self, seeded, storage):
        """A NULL tenant has always meant platform-shared here. Skipping those
        rows would leave them queued forever on every multi-tenant install."""
        doc = seeded["docs"][1]
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=None, requested_by="ann")
            out = await index_queue.run_due_jobs(db, tenant_id="acme")

        assert out["claimed"] == 1


# ── The window on the queue ──────────────────────────────────────────────────


class TestTheApi:
    async def test_the_jobs_of_a_case_are_listed_with_their_reason(
            self, api, seeded, storage):
        """A queue with no window on it is indistinguishable from a document
        that was silently dropped."""
        case_id = seeded["case"].id
        await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                       headers=_headers(), json={"background": True})
        r = await api.get(f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs",
                          headers=_headers())
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 2
        assert {j["status"] for j in r.json()["jobs"]} == {"pending"}
        assert "last_error" in r.json()["jobs"][0]

    async def test_a_pending_job_can_be_cancelled_and_the_document_requeued(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                       headers=_headers(), json={"background": True})
        listed = (await api.get(
            f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs",
            headers=_headers())).json()["jobs"]

        r = await api.post(
            f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs/"
            f"{listed[0]['id']}/cancel", headers=_headers())
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled"

        async with TestSessionFactory() as db:
            out = await index_queue.run_due_jobs(db)
        assert out["claimed"] == 1, "the cancelled job must not be drained"

    async def test_a_job_that_already_ran_cannot_be_cancelled(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                       headers=_headers(), json={"background": True})
        async with TestSessionFactory() as db:
            await index_queue.run_due_jobs(db)
        listed = (await api.get(
            f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs",
            headers=_headers())).json()["jobs"]

        r = await api.post(
            f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs/"
            f"{listed[0]['id']}/cancel", headers=_headers())
        assert r.status_code == 409, r.text

    async def test_a_job_id_from_another_case_is_absent_not_forbidden(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        r = await api.post(
            f"/api/v1/hxnexus/cases/{case_id}/documents/index-jobs/"
            f"{uuid.uuid4()}/cancel", headers=_headers())
        assert r.status_code == 404, r.text

    async def test_the_queue_is_not_readable_without_case_access(
            self, api, seeded, storage):
        """The same gate as indexing. A job row names a document on the case."""
        tenancy.clear_cache()
        async with TestSessionFactory() as db:
            db.add(TenantModel(id=uuid.uuid4(), slug="beta", name="Beta"))
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ben",
                                      tenant_id="beta"))
            await db.commit()
        r = await api.get(
            f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index-jobs",
            headers=_headers("ben"))
        assert r.status_code == 404, r.text


# ── The scheduler action ─────────────────────────────────────────────────────


class TestTheSchedulerAction:
    async def test_the_kind_is_offered_by_the_catalogue(self):
        """The editor is generated from the handlers, so a kind that is not in
        the registry cannot be scheduled at all."""
        assert "document_index" in scheduler_actions.kinds()
        assert any(a["kind"] == "document_index"
                   for a in scheduler_actions.catalogue())

    async def test_a_nonsense_bound_is_refused_at_validate(self, seeded):
        action = scheduler_actions.get("document_index")
        schedule = type("S", (), {"tenant_id": None})()
        async with TestSessionFactory() as db:
            with pytest.raises(scheduler_actions.ScheduleActionError):
                await action.validate(db, schedule, {"max_jobs": 0})
            with pytest.raises(scheduler_actions.ScheduleActionError):
                await action.validate(db, schedule, {"budget_seconds": "soon"})
            await action.validate(db, schedule, {})

    async def test_the_action_drains_what_the_route_queued(
            self, api, seeded, storage):
        case_id = seeded["case"].id
        await api.post(f"/api/v1/hxnexus/cases/{case_id}/documents/index",
                       headers=_headers(), json={"background": True})
        action = scheduler_actions.get("document_index")
        schedule = type("S", (), {"tenant_id": "acme"})()
        async with TestSessionFactory() as db:
            out = await action.run(db, schedule, {})
        assert out["indexed"] == 2

    async def test_an_empty_queue_is_reported_as_empty_not_as_zero_work(
            self, seeded):
        action = scheduler_actions.get("document_index")
        schedule = type("S", (), {"tenant_id": None})()
        async with TestSessionFactory() as db:
            out = await action.run(db, schedule, {})
        assert "background: true" in out["note"]


# ── The operator's window: tenant-wide list and retry (2026-09-17) ───────────


class TestTheQueueScreen:
    """What Studio's Index queue tab reads. Pinned: it is scoped to the tenant
    THROUGH THE CASE, it says when nothing drains the queue, and a retry is a
    new job that leaves the failure on the record."""

    async def _fail_one(self, seeded, storage):
        doc = seeded["docs"][0]
        storage.fail_with = RuntimeError("scanner garbage")
        async with TestSessionFactory() as db:
            await index_queue.enqueue_document(
                db, document_id=doc.id, case_id=seeded["case"].id,
                tenant_id=str(seeded["tenant"].id), requested_by="ann")
            for _ in range(3):
                await index_queue.run_due_jobs(db)
            [row] = await _jobs(db)
        storage.fail_with = None
        assert row.status == "failed"
        return row

    async def test_the_tenant_list_shows_every_case_with_names_and_counts(
            self, api, seeded, storage):
        await api.post(f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index",
                       headers=_headers(), json={"background": True})
        r = await api.get("/api/v1/hxnexus/documents/index-jobs", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["counts"] == {"pending": 2}
        assert {j["filename"] for j in body["jobs"]} == {"scan.txt", "second.txt"}
        assert {j["case_number"] for j in body["jobs"]} == {"ACME-Q1"}

    async def test_it_says_when_no_schedule_drains_the_queue(self, api, seeded):
        r = await api.get("/api/v1/hxnexus/documents/index-jobs", headers=_headers())
        assert r.json()["drain_scheduled"] is False

    async def test_an_active_schedule_is_seen_by_its_slug(self, api, seeded):
        from velaris_shared.db.models import ScheduleModel

        async with TestSessionFactory() as db:
            db.add(ScheduleModel(
                id=uuid.uuid4(), tenant_id="acme", name="drain", cron="*/5 * * * *",
                action_kind="document_index", action_config={}, status="active",
                created_by="ann"))
            await db.commit()
        r = await api.get("/api/v1/hxnexus/documents/index-jobs", headers=_headers())
        assert r.json()["drain_scheduled"] is True

    async def test_another_tenants_jobs_are_not_listed(self, api, seeded, storage):
        await api.post(f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index",
                       headers=_headers(), json={"background": True})
        tenancy.clear_cache()
        async with TestSessionFactory() as db:
            db.add(TenantModel(id=uuid.uuid4(), slug="beta", name="Beta"))
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ben", tenant_id="beta"))
            await db.commit()
        r = await api.get("/api/v1/hxnexus/documents/index-jobs", headers=_headers("ben"))
        assert r.status_code == 200, r.text
        assert r.json()["jobs"] == [] and r.json()["counts"] == {}

    async def test_a_case_worker_may_not_read_the_whole_queue(self, api, seeded):
        tok = _token("ann", roles=("case_worker",))
        r = await api.get("/api/v1/hxnexus/documents/index-jobs",
                          headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, r.text

    async def test_a_failed_job_is_retried_as_a_new_job(self, api, seeded, storage):
        failed = await self._fail_one(seeded, storage)
        r = await api.post(
            f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index-jobs/"
            f"{failed.id}/retry", headers=_headers())
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending"
        assert r.json()["retry_of"] == str(failed.id)
        async with TestSessionFactory() as db:
            rows = await _jobs(db)
        assert [x.status for x in rows] == ["failed", "pending"]
        assert "scanner garbage" in rows[0].last_error, "the failure stays on record"

    async def test_a_pending_job_is_not_retried(self, api, seeded, storage):
        await api.post(f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index",
                       headers=_headers(), json={"background": True})
        async with TestSessionFactory() as db:
            job = (await _jobs(db))[0]
        r = await api.post(
            f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index-jobs/"
            f"{job.id}/retry", headers=_headers())
        assert r.status_code == 409, r.text


async def test_a_caller_with_no_tenant_is_the_default_tenant_not_every_tenant(api, seeded):
    """Found in the 2026-09-17 audit: a missing tenant claim was read as
    unscoped, so an admin token with no tenant listed every tenant's queue."""
    await api.post(f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index",
                   headers=_headers(), json={"background": True})
    # An admin (not a superadmin) with no directory row, so no tenant claim.
    r = await api.get("/api/v1/hxnexus/documents/index-jobs",
                      headers={"Authorization": f"Bearer {_token('nobody-in-particular')}"})
    assert r.status_code == 200, r.text
    assert r.json()["jobs"] == [], "acme's jobs must not reach a caller outside acme"
