"""Audit streaming — this platform's security events, in the customer's SIEM.

**The gap this closes.** The audit trail was always good and had no way out.
`security_events` is the chokepoint every authentication, authorization
decision, data access and configuration change funnels through, and all of it
stayed in this platform's own database. To an enterprise SOC that is a finding,
not an answer: an attacker who reaches the platform reaches its logs too, and a
security team cannot correlate what it never receives.

**Plainly named, deliberately.** No `Hx` prefix: there is no AI anywhere in
this component. It reads rows and posts them.

**Shape.**

* A **reader on a cursor**, never a hook on the write path. `log_security_event`
  is called from inside login and inside every authorization denial; a network
  call there would make a SIEM outage a login outage — the exact failure the SOC
  is watching for.
* **At-least-once.** The cursor advances only after a batch is accepted. A
  duplicate in a SIEM costs a deduplication rule; a gap costs an incident
  nobody sees.
* **On a schedule, not a poller.** A `audit_stream` scheduler action kind, the
  same choice `calendar_feed` made: a second always-on loop is a second thing to
  configure, forget, and hide a stall in.
* **Off by default.** No install has a target. Creating one and enabling one are
  separate acts.

**Why the SSRF rules here are NOT the calendar's.** `calendar/feeds.py` refuses
any URL resolving to a private address, because there the URL comes from an end
user. A SIEM is almost always ON the customer's own private network — refusing
`10.0.0.0/8` would refuse the normal case. What replaces that control is who may
write the target: `audit.stream.write`, an administrator's privilege, the same
trust level as configuring a connector. The endpoint is recorded with the row so
"where is our audit going" always has an answer.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.audit_stream.forwarder import (  # noqa: F401
    AuditStreamError,
    forward_target,
    run_due_targets,
)
from case_service.audit_stream.format import format_batch  # noqa: F401
