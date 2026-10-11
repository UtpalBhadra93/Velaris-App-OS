"""The field node — the device half of F-10 (Oct item 8).

`python -m velaris_shared.field_node --help`. See `node.py` for what it
enforces on the device and `deploy/field-node/README.md` for how to run one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from velaris_shared.field_node.node import FieldNode, NodeError, Stale  # noqa: F401
