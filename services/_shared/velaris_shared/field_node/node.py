"""The field node: what a device holds while it is away, and what it refuses.

F-10's hub half (09-04) verifies, merges and shreds. This is the other half —
the thing that goes out into a field, a ship, a clinic with no line — so that
the hub's checks have something real on the far side of them.

What it keeps, in ONE SQLite file (mode 0600, directory 0700):

* its **Ed25519 key** — generated here, never leaves; the hub pins the public
  half at registration and every contact and sync is signed with it;
* the **bundle** it was dispatched: the projected cases (only what the node's
  ceiling admitted), the offline deny-list, the sub-chain genesis, `stale_after`;
* the **events** a worker records, chained from the genesis with the SAME
  `subchain` code the hub verifies with — one implementation, so the two sides
  cannot come to disagree about what was hashed.

What it enforces, on the device, before the hub ever sees anything:

* **Fail closed past `stale_after`.** Reading a case, recording work and asking
  the local model all stop. Syncing, checking in and wiping do not — those are
  how a stale node gets back to being a good one.
* **The clock may not go backwards** past a small tolerance. The simplest way
  round a staleness lockout is to set the clock back, so the node remembers the
  latest time it has seen and refuses to act on an earlier one.
* **`offline_deny`** — a namespace the hub denied (`export` by default) is
  refused here, not merely frowned on at sync.
* **Nothing new while work is unsynced.** Taking a new bundle supersedes the old
  dispatch on the hub, which makes the unsynced work unsyncable forever; so a
  node with unsynced events refuses a new bundle until it has synced.
* **A synced dispatch is dropped.** After the hub accepts a sync the cases and
  events are deleted: a dispatch is synced once, and data a node can no longer
  act on is data it should not be carrying.

What it does NOT protect against, stated so nobody assumes it: a person with
root on the device can read the file and edit its clock high-water mark. The
defences there are disk encryption (see `deploy/field-node/README.md`), the
hub refusing work recorded after `stale_after`, and the shred order.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from velaris_shared.field import subchain

#: Kept in step with `case_service.field.service.EVENT_KINDS` (pinned by a
#: test): a node may say what a worker did to a case, nothing else.
EVENT_KINDS: tuple[str, ...] = ("variable_set", "note", "step_complete")

#: How far back the clock may step before the node stops trusting it. NTP
#: corrections are seconds; somebody undoing a lockout is hours.
CLOCK_TOLERANCE = timedelta(minutes=5)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (case_id TEXT PRIMARY KEY, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
    target TEXT, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    node_time TEXT NOT NULL, prev_hash TEXT NOT NULL, content_hash TEXT NOT NULL);
"""


class NodeError(Exception):
    """The node refuses. The message is for the person holding the device."""


class Stale(NodeError):
    """The bundle is past `stale_after`: sync, or check in, before acting again."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fingerprint(public_key_b64: str) -> str:
    """The hub's `key_fingerprint`: what an operator reads back at registration."""
    import hashlib
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:32]


def contact_message(slug: str, key_id: str, at: str) -> str:
    """Must equal `case_service.field.service.contact_message` (pinned by a test)."""
    return f"velaris-field-contact:{slug}:{key_id}:{at}"


class FieldNode:
    """One device's state. Every method that shows or changes case data checks
    freshness first; the ones that bring the node home do not."""

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self._clock = clock or _utcnow
        if not self.path.exists():
            raise NodeError(f"no node at {self.path}; run `init` first")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise NodeError(
                f"{self.path} is readable by other users (mode {oct(mode)}); it holds "
                f"case data and the node's key. Run: chmod 600 {self.path}")
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        # Deleted rows are overwritten, not just unlinked from the page: what
        # a sync or a wipe removes is not left readable in free pages.
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.execute("PRAGMA journal_mode = DELETE")
        self._db.executescript(_SCHEMA)

    # ── creation and identity ────────────────────────────────────────────────

    @classmethod
    def init(cls, path: str | Path, *, slug: str, hub_url: str,
             clock: Callable[[], datetime] | None = None) -> "FieldNode":
        """A new node with a new key. Refuses to overwrite one: a second `init`
        over a live node would orphan its key, and with it its unsynced work."""
        p = Path(path)
        if p.exists():
            raise NodeError(f"{p} already exists; a node is initialised once")
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        node = cls(p, clock=clock)
        key = ed25519.Ed25519PrivateKey.generate()
        raw = key.private_bytes(encoding=serialization.Encoding.Raw,
                                format=serialization.PrivateFormat.Raw,
                                encryption_algorithm=serialization.NoEncryption())
        node._set("private_key", base64.b64encode(raw).decode())
        node._set("slug", slug)
        node._set("hub_url", hub_url.rstrip("/"))
        node._set("clock_high_water", node._clock().isoformat())
        return node

    def _get(self, key: str) -> str | None:
        row = self._db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM meta WHERE k = ?", (key,))
        else:
            self._db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    def _key(self) -> ed25519.Ed25519PrivateKey:
        raw = self._get("private_key")
        if not raw:
            raise NodeError("this node has no key; it has been wiped")
        return ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))

    @property
    def slug(self) -> str:
        return self._get("slug") or ""

    @property
    def hub_url(self) -> str:
        return self._get("hub_url") or ""

    @property
    def public_key(self) -> str:
        raw = self._key().public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    @property
    def key_id(self) -> str:
        return fingerprint(self.public_key)

    def sign(self, message: str) -> str:
        return base64.b64encode(self._key().sign(message.encode())).decode()

    def signed_contact(self) -> dict[str, str]:
        at = self._clock().isoformat()
        return {"at": at, "signature": self.sign(contact_message(self.slug, self.key_id, at))}

    # ── the clock and the lockout ────────────────────────────────────────────

    def _now(self) -> datetime:
        """The time, refusing one earlier than the node has already seen."""
        now = self._clock()
        seen = self._get("clock_high_water")
        if seen and now < _parse(seen) - CLOCK_TOLERANCE:
            raise NodeError(
                f"the clock reads {now.isoformat()}, earlier than {seen} which this "
                f"node has already seen. It will not act on a clock that went "
                f"backwards; correct the time, then sync")
        if not seen or now > _parse(seen):
            self._set("clock_high_water", now.isoformat())
        return now

    def staleness(self) -> dict[str, Any]:
        after = self._get("stale_after")
        if not after:
            return {"stale": False, "reason": "no bundle held"}
        now = self._clock()
        return {"stale": now > _parse(after), "stale_after": after}

    def _require_fresh(self) -> datetime:
        now = self._now()
        if not self._get("dispatch_id"):
            raise NodeError("this node holds no bundle; pull or import one")
        after = _parse(self._get("stale_after") or "")
        if now > after:
            raise Stale(
                f"this node's snapshot went stale at {after.isoformat()}. It has "
                f"stopped acting on it: sync, then take a new bundle")
        return now

    def _deny(self, namespace: str) -> None:
        deny = json.loads(self._get("offline_deny") or "[]")
        if namespace in deny:
            raise NodeError(f"{namespace!r} is denied to this node while offline "
                            f"(the hub's offline_deny for this dispatch)")

    # ── the bundle ───────────────────────────────────────────────────────────

    def unsynced(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def load_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """Take a dispatch bundle, from the hub or from a file carried in."""
        self._now()
        for key in ("dispatch_id", "cases", "subchain_genesis", "stale_after", "offline_deny"):
            if key not in bundle:
                raise NodeError(f"this is not a dispatch bundle (no {key!r})")
        dest = (bundle.get("node") or {}).get("slug")
        if dest != self.slug:
            raise NodeError(f"this bundle was dispatched to {dest!r}, not to {self.slug!r}")
        if self.unsynced():
            raise NodeError(
                f"{self.unsynced()} recorded events have not been synced. A new "
                f"bundle supersedes the old dispatch on the hub and that work could "
                f"never be handed back; sync first")
        _parse(bundle["stale_after"])
        self._db.execute("BEGIN")
        try:
            self._db.execute("DELETE FROM cases")
            for case in bundle["cases"]:
                self._db.execute("INSERT INTO cases (case_id, doc) VALUES (?, ?)",
                                 (str(case["case_id"]), json.dumps(case)))
            self._set("dispatch_id", str(bundle["dispatch_id"]))
            self._set("subchain_genesis", str(bundle["subchain_genesis"]))
            self._set("head", str(bundle["subchain_genesis"]))
            self._set("stale_after", str(bundle["stale_after"]))
            self._set("offline_deny", json.dumps(sorted(bundle["offline_deny"] or [])))
            self._set("ceiling", str((bundle.get("node") or {}).get("ceiling") or ""))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return {"dispatch_id": bundle["dispatch_id"], "cases": len(bundle["cases"]),
                "stale_after": bundle["stale_after"]}

    # ── reading and recording ────────────────────────────────────────────────

    def cases(self) -> list[dict[str, Any]]:
        self._require_fresh()
        out = []
        for (doc,) in self._db.execute("SELECT doc FROM cases ORDER BY case_id"):
            c = json.loads(doc)
            out.append({"case_id": c["case_id"], "status": c.get("status"),
                        "priority": c.get("priority"), "stage": c.get("current_stage_id")})
        return out

    def show(self, case_id: str) -> dict[str, Any]:
        self._require_fresh()
        return self._case(case_id)

    def _case(self, case_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT doc FROM cases WHERE case_id = ?",
                               (str(case_id),)).fetchone()
        if row is None:
            raise NodeError("that case is not on this node")
        return json.loads(row[0])

    def record(self, case_id: str, kind: str, *, target: str | None = None,
               payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one event to the chain. The only way work enters the node."""
        now = self._require_fresh()
        if kind not in EVENT_KINDS:
            raise NodeError(f"a node records {', '.join(EVENT_KINDS)}; not {kind!r}")
        self._case(case_id)
        if kind == "variable_set" and not target:
            raise NodeError("setting a field needs the field's name")
        payload = payload or {}
        seq = self.unsynced() + 1
        prev = self._get("head") or ""
        event = {"seq": seq, "case_id": str(case_id), "kind": kind, "target": target,
                 "payload": payload, "payload_sha256": subchain.payload_hash(payload),
                 "node_time": now.isoformat()}
        event["prev_hash"] = prev
        event["content_hash"] = subchain.link(subchain.event_document(event), prev)
        self._db.execute("BEGIN")
        try:
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, event["case_id"], kind, target, json.dumps(payload),
                 event["payload_sha256"], event["node_time"], prev, event["content_hash"]))
            self._set("head", event["content_hash"])
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        return event

    def note(self, case_id: str, text: str) -> dict[str, Any]:
        return self.record(case_id, "note", payload={"text": text})

    def set_field(self, case_id: str, name: str, value: Any) -> dict[str, Any]:
        return self.record(case_id, "variable_set", target=name, payload={"value": value})

    def complete(self, case_id: str, step: str) -> dict[str, Any]:
        return self.record(case_id, "step_complete", target=step, payload={"step": step})

    def export(self, case_id: str) -> dict[str, Any]:
        """Hand a case's data to something outside the node. Denied by default."""
        self._require_fresh()
        self._deny("export")
        return self._case(case_id)

    def assist(self, case_id: str, question: str, *, url: str, model: str,
               post: Callable[..., str] | None = None) -> str:
        """Ask a LOCAL model about a case. See `assist.ask` for what "local" means."""
        from velaris_shared.field_node import assist

        self._require_fresh()
        self._deny("assist")
        case = self._case(case_id)
        return assist.ask(url=url, model=model, case=case, question=question, post=post)

    # ── going home ───────────────────────────────────────────────────────────

    def events(self) -> list[dict[str, Any]]:
        out = []
        for row in self._db.execute(
                "SELECT seq, case_id, kind, target, payload, payload_sha256, node_time, "
                "prev_hash, content_hash FROM events ORDER BY seq"):
            out.append({"seq": row[0], "case_id": row[1], "kind": row[2], "target": row[3],
                        "payload": json.loads(row[4]), "payload_sha256": row[5],
                        "node_time": row[6], "prev_hash": row[7], "content_hash": row[8]})
        return out

    def sync_body(self) -> dict[str, Any]:
        """What the hub's sync takes. Always signed: the signature is optional on
        the hub (off by default), but a node that always signs is a node an
        operator can switch the requirement on for."""
        self._now()
        dispatch_id = self._get("dispatch_id")
        if not dispatch_id:
            raise NodeError("this node holds no bundle, so there is nothing to sync")
        head = self._get("head") or ""
        return {"dispatch_id": dispatch_id, "events": self.events(),
                "signature": self.sign(head)}

    def synced(self, result: dict[str, Any]) -> None:
        """The hub accepted the stream: drop the dispatch and everything in it."""
        if not result.get("verified"):
            raise NodeError("the hub did not verify this stream; nothing was dropped")
        self._db.execute("BEGIN")
        self._db.execute("DELETE FROM events")
        self._db.execute("DELETE FROM cases")
        for key in ("dispatch_id", "subchain_genesis", "head", "stale_after",
                    "offline_deny", "ceiling"):
            self._set(key, None)
        self._set("last_synced_head", str(result.get("head") or ""))
        self._db.execute("COMMIT")
        self._db.execute("VACUUM")

    def wipe(self) -> None:
        """Destroy everything: cases, events, the key. The file is overwritten
        and removed. On flash storage an overwrite is not a guarantee — which is
        why the README asks for disk encryption underneath."""
        size = self.path.stat().st_size
        self._db.execute("DELETE FROM events")
        self._db.execute("DELETE FROM cases")
        self._db.execute("DELETE FROM meta")
        self._db.execute("VACUUM")
        self._db.close()
        with open(self.path, "r+b") as fh:
            fh.write(b"\0" * max(size, self.path.stat().st_size))
            fh.flush()
            os.fsync(fh.fileno())
        self.path.unlink()
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

    def status(self) -> dict[str, Any]:
        """What the node is, never what it holds."""
        return {"slug": self.slug, "key_id": self.key_id, "hub_url": self.hub_url,
                "dispatch_id": self._get("dispatch_id"),
                "cases": int(self._db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]),
                "unsynced_events": self.unsynced(),
                "offline_deny": json.loads(self._get("offline_deny") or "[]"),
                "staleness": self.staleness(),
                "last_synced_head": self._get("last_synced_head")}

    def close(self) -> None:
        self._db.close()
