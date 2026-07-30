"""Voice form-fill (voice input P3) — spoken words to candidate field values.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from .service import build_catalogue, extract, load_transcript

__all__ = ["build_catalogue", "extract", "load_transcript"]
