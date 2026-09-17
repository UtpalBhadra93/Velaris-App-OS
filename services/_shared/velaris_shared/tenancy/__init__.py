"""Tenant identity, shared by every service.

Lives here rather than in case-service because `hxguard.require_case` — which
is in this package — has to be able to ask "is this case even in the caller's
tenant?". See `identity` for why the question is harder than it sounds.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
