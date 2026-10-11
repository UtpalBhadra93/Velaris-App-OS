"""Re-export. The sub-chain moved to `velaris_shared.field.subchain` (Oct item 8)
so the hub and the field node import ONE implementation — "a second
implementation of what is hashed is how two sides come to disagree about
whether a log was tampered with". Every existing import keeps working.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from velaris_shared.field.subchain import (  # noqa: F401
    build,
    event_document,
    link,
    payload_hash,
    verify,
)
