"""Oct item 7 / migration 206 — `POST /hxnexus/documents/upload` can index in the background.

What these hold down:

* **the default is unchanged** — no `background` means indexed in the request,
  no queue row, nothing stored;
* a background upload parks the bytes, queues ONE job and indexes nothing yet;
* the drain indexes from the parked bytes and then **deletes them** — on done,
  on failing for good, on cancel, and when the case is anonymized first;
* it needs a case (a case-less job would be invisible) and the same case
  access as the synchronous path;
* a staged job cannot be "retried" into a job with nothing to read.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from velaris_shared.db.models import DocumentChunkModel, DocumentIndexJobModel

from case_service.hxnexus import index_queue
from tests.conftest import TestSessionFactory
from tests.test_document_index_queue import (  # noqa: F401 — fixtures
    _FakeStorage, _headers, api, seeded,
)

pytestmark = pytest.mark.asyncio

BODY = b"the quick brown fox jumps over the lazy dog " * 20


class _StagingStorage(_FakeStorage):
    def __init__(self):
        super().__init__()
        self.deleted: list[str] = []

    async def put(self, key, data, content_type="application/octet-stream"):
        self.blobs[key] = data

    async def delete(self, key):
        self.deleted.append(key)
        self.blobs.pop(key, None)


@pytest.fixture
def storage(monkeypatch):
    fake = _StagingStorage()
    monkeypatch.setattr("case_service.storage.get_storage_backend", lambda: fake)
    monkeypatch.setattr("case_service.api.routers.hxnexus.get_storage_backend", lambda: fake)
    return fake


async def _upload(api, case_id=None, *, background=None, where="form"):
    data, params = {}, {}
    if case_id is not None:
        data["case_id"] = str(case_id)
    if background is not None:
        (data if where == "form" else params)["background"] = "true" if background else "false"
    return await api.post("/api/v1/hxnexus/documents/upload", headers=_headers(),
                          params=params, data=data,
                          files={"file": ("scan.txt", BODY, "text/plain")})


async def _staged_jobs():
    async with TestSessionFactory() as db:
        return (await db.execute(select(DocumentIndexJobModel).where(
            DocumentIndexJobModel.staged_storage_key.is_not(None)))).scalars().all()


async def _chunks(document_id):
    async with TestSessionFactory() as db:
        return (await db.execute(select(DocumentChunkModel).where(
            DocumentChunkModel.document_id == document_id))).scalars().all()


class TestDefaultUnchanged:
    async def test_no_flag_indexes_in_the_request_and_stores_nothing(self, api, seeded, storage):
        r = await _upload(api, seeded["case"].id)
        assert r.status_code == 200, r.text
        assert r.json()["chunks_indexed"] > 0
        assert "queued" not in r.json()
        assert await _staged_jobs() == []
        assert not [k for k in storage.blobs if k.startswith("hxnexus/staged-uploads/")]

    async def test_explicit_false_is_the_default(self, api, seeded, storage):
        r = await _upload(api, seeded["case"].id, background=False)
        assert r.json()["chunks_indexed"] > 0
        assert await _staged_jobs() == []


class TestBackgroundUpload:
    @pytest.mark.parametrize("where", ["form", "query"])
    async def test_queues_one_job_and_indexes_nothing_yet(self, api, seeded, storage, where):
        r = await _upload(api, seeded["case"].id, background=True, where=where)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["queued"] is True and body["job"]["staged_upload"] is True
        doc_id = uuid.UUID(body["document_id"])

        jobs = await _staged_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.status == "pending" and job.case_id == seeded["case"].id
        assert str(job.tenant_id) == str(seeded["tenant"].id)     # the CASE's tenant
        assert job.staged_filename == "scan.txt"
        assert storage.blobs[job.staged_storage_key] == BODY
        assert await _chunks(doc_id) == []

    async def test_drain_indexes_then_deletes_the_bytes(self, api, seeded, storage):
        r = await _upload(api, seeded["case"].id, background=True)
        doc_id = uuid.UUID(r.json()["document_id"])
        key = (await _staged_jobs())[0].staged_storage_key

        async with TestSessionFactory() as db:
            out = await index_queue.run_due_jobs(db)
        assert out["indexed"] == 1, out

        job = (await _staged_jobs())[0]
        assert job.status == "done" and job.chunks_indexed > 0
        chunks = await _chunks(doc_id)
        assert chunks and all(c.case_id == seeded["case"].id for c in chunks)
        assert key in storage.deleted and key not in storage.blobs

    async def test_listed_with_its_filename(self, api, seeded, storage):
        await _upload(api, seeded["case"].id, background=True)
        r = await api.get("/api/v1/hxnexus/documents/index-jobs", headers=_headers())
        assert r.status_code == 200, r.text
        staged = [j for j in r.json()["jobs"] if j["staged_upload"]]
        assert staged and staged[0]["filename"] == "scan.txt"

    async def test_needs_a_case(self, api, seeded, storage):
        r = await _upload(api, None, background=True)
        assert r.status_code == 400
        assert await _staged_jobs() == []
        assert not [k for k in storage.blobs if k.startswith("hxnexus/staged-uploads/")]

    async def test_same_case_access_as_sync(self, api, seeded, storage):
        r = await _upload(api, uuid.uuid4(), background=True)
        assert r.status_code == 404
        assert await _staged_jobs() == []


class TestTheBytesDoNotOutliveTheJob:
    async def test_failing_for_good_deletes_them(self, api, seeded, storage):
        await _upload(api, seeded["case"].id, background=True)
        key = (await _staged_jobs())[0].staged_storage_key
        async with TestSessionFactory() as db:
            job = (await _staged_jobs())[0]
            await db.execute(DocumentIndexJobModel.__table__.update()
                             .where(DocumentIndexJobModel.id == job.id).values(max_attempts=1))
            await db.commit()
        storage.fail_with = OSError("disk gone")
        async with TestSessionFactory() as db:
            await index_queue.run_due_jobs(db)
        assert (await _staged_jobs())[0].status == "failed"
        assert key in storage.deleted

    async def test_a_retryable_failure_keeps_them(self, api, seeded, storage):
        await _upload(api, seeded["case"].id, background=True)
        key = (await _staged_jobs())[0].staged_storage_key
        storage.fail_with = OSError("blip")
        async with TestSessionFactory() as db:
            await index_queue.run_due_jobs(db)
        assert (await _staged_jobs())[0].status == "pending"
        assert key in storage.blobs and key not in storage.deleted

    async def test_cancel_deletes_them(self, api, seeded, storage):
        r = await _upload(api, seeded["case"].id, background=True)
        job_id = r.json()["job"]["id"]
        key = (await _staged_jobs())[0].staged_storage_key
        c = await api.post(
            f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index-jobs/{job_id}/cancel",
            headers=_headers())
        assert c.status_code == 200, c.text
        assert key in storage.deleted

    async def test_retry_is_refused_with_the_reason(self, api, seeded, storage):
        r = await _upload(api, seeded["case"].id, background=True)
        job_id = r.json()["job"]["id"]
        base = f"/api/v1/hxnexus/cases/{seeded['case'].id}/documents/index-jobs/{job_id}"
        await api.post(base + "/cancel", headers=_headers())
        rr = await api.post(base + "/retry", headers=_headers())
        assert rr.status_code == 409 and "upload it again" in rr.text

    async def test_anonymizing_the_case_cancels_and_deletes(self, api, seeded, storage):
        """Through the real retention purge: a closed, old case is anonymized
        while its upload is still waiting for a drain that may never come."""
        from datetime import datetime, timedelta, timezone

        from velaris_shared.db.models import CaseInstanceModel
        from case_service.compliance.retention import _anonymize_cases

        await _upload(api, seeded["case"].id, background=True)
        key = (await _staged_jobs())[0].staged_storage_key
        async with TestSessionFactory() as db:
            await db.execute(CaseInstanceModel.__table__.update()
                             .where(CaseInstanceModel.id == seeded["case"].id)
                             .values(status="closed",
                                     updated_at=datetime.now(timezone.utc) - timedelta(days=400)))
            await db.commit()
        async with TestSessionFactory() as db:
            n = await _anonymize_cases(db, datetime.now(timezone.utc) - timedelta(days=30), True)
            await db.commit()
        assert n >= 1
        job = (await _staged_jobs())[0]
        assert job.status == "cancelled" and job.active_key is None
        assert key in storage.deleted and key not in storage.blobs


class TestNothingIsOrphaned:
    async def test_storage_failure_leaves_a_cancelled_job_and_no_bytes(self, api, seeded, storage, monkeypatch):
        async def boom(key, data, content_type="application/octet-stream"):
            raise OSError("bucket gone")
        monkeypatch.setattr(storage, "put", boom)
        r = await _upload(api, seeded["case"].id, background=True)
        assert r.status_code == 503
        jobs = await _staged_jobs()
        assert len(jobs) == 1 and jobs[0].status == "cancelled"
        assert jobs[0].active_key is None and "could not be stored" in jobs[0].last_error
        assert not [k for k in storage.blobs if k.startswith("hxnexus/staged-uploads/")]

    async def test_a_job_whose_bytes_never_arrived_fails_visibly(self, api, seeded, storage):
        """The crash window: the row exists, the bytes do not."""
        await _upload(api, seeded["case"].id, background=True)
        job = (await _staged_jobs())[0]
        storage.blobs.pop(job.staged_storage_key)
        async with TestSessionFactory() as db:
            await db.execute(DocumentIndexJobModel.__table__.update()
                             .where(DocumentIndexJobModel.id == job.id).values(max_attempts=1))
            await db.commit()
        async with TestSessionFactory() as db:
            await index_queue.run_due_jobs(db)
        job = (await _staged_jobs())[0]
        assert job.status == "failed" and job.last_error
