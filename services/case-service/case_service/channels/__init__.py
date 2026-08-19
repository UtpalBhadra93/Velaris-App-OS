"""Omnichannel inbound — one channel spine for SMS, WhatsApp and voice metadata.

`email_messages` already proves the bidirectional, nullable-case, threaded shape
works, so these tables are modelled on it rather than invented; `sms_messages`
stays as it is, recording step-bound outbound sends.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.channels import optout, service, signatures  # noqa: F401

__all__ = ["optout", "service", "signatures"]
