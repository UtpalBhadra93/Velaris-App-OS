"""RPA / bot runner — automating systems that have no API.

A bot is a container that performs a task and returns a result. It runs on the
Marketplace Layer-2 substrate unchanged, because an RPA bot is a different kind
of tenant of that substrate rather than a different substrate.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.bots import credentials, service, tokens  # noqa: F401

__all__ = ["credentials", "service", "tokens"]
