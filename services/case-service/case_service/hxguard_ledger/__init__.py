"""The imperative-authorization ledger — a scanner over this service's routers.

It lived in `hxguard/` until the 13b carve-out. It stayed behind because it
reads `api/routers` from disk: that is this application's layout, not something
the shared authorization layer may know about.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
