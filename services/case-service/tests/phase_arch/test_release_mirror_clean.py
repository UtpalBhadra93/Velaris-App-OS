"""The release twin carries source and nothing else.

**Why this exists.** `Velaris/` is what ships. Twice on 2026-08-27 it held things
it should not: 57 `__pycache__` directories and 338 `.pyc` files, left by running
this very suite *inside* the mirror to prove a sync worked; and a 12 MB
`studio/dist`, a build of somebody's dev box.

The second one was not merely weight. `deploy/systemd/velaris-studio.service`
runs `ExecStartPre=test -d dist || npm run build`, so a `dist` that arrives with
the code is trusted **forever** — a client would serve that stale bundle and
never build their own. A shipped build artefact is worse than a missing one,
because the missing one repairs itself on first start.

**Why a test rather than an exclude or a `.gitignore`.**

`rsync --exclude=dist` filters the *source* side. It does not remove a `dist`
already sitting in the destination, and `--delete` never even considers a path
the filter has hidden — so the exclude list reads like protection and is not.

`Velaris/.gitignore` does list these, which is why none of it was ever committed.
That is exactly the problem: **git hid them, so nothing said they were there.**
This looks at the working tree, which is what a sync, a `docker build` or a copy
would actually take.

Skips when there is no mirror — a clean checkout and CI have no `Velaris/`, and a
guard that fails where the thing it guards does not exist is a guard people turn
off.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
MIRROR = ROOT / "Velaris"

#: Directory names that are build or runtime state, never a distribution.
#: `.venv` alone once reached 321 MB against ~11 MB of real source.
FORBIDDEN_DIRS = {
    "__pycache__", ".venv", "venv", "node_modules", "dist", ".pytest_cache",
    ".ruff_cache", ".vite", "historical",
}

#: Suffixes with the same problem, plus the one that is a security matter:
#: `deploy/tls/certs/` holds a real CA key and one private key per service, and
#: they are gitignored in helix precisely because they are this machine's.
FORBIDDEN_SUFFIXES = {".pyc", ".log", ".key", ".pem", ".egg-info"}

#: Credentials with NO suffix, which the sweep above cannot see.
#:
#: Added 2026-09-09. A mirror sync dry-run was about to copy `deploy/tls/certs/`
#: — this box's CA key and one private key per service — and the suffix rule
#: would have caught that. It would NOT have caught these: OpenBao's AppRole
#: credentials are extensionless, and `secret_id` is the credential half of that
#: pair. They were in the mirror from a June sync, untracked and gitignored, so
#: nothing had ever reported them.
#:
#: Matched by exact NAME rather than by a `secret`/`id` substring, because a
#: substring rule over a source tree hits `secrets-init.sh`, `role_ids.py` and
#: every other honest file that happens to say the word.
FORBIDDEN_NAMES = {"secret_id", "role_id", ".bao-init.json"}

#: `Velaris/.git` is the mirror's own repository and `fonts/` is mirror-only
#: content with no source in helix. Neither is ours to police.
SKIP_TREES = {".git", "fonts"}


def _walk():
    """Every path in the mirror, minus the trees that are not ours."""
    stack = [MIRROR]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, FileNotFoundError):       # pragma: no cover
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_TREES and entry.parent == MIRROR:
                    continue
                yield entry
                if entry.name not in FORBIDDEN_DIRS:
                    stack.append(entry)
            else:
                yield entry


pytestmark = pytest.mark.skipif(
    not MIRROR.is_dir(), reason="no release mirror in this checkout")


def test_the_mirror_carries_no_build_or_runtime_state():
    found = sorted(
        str(p.relative_to(MIRROR))
        for p in _walk()
        if (p.is_dir() and p.name in FORBIDDEN_DIRS)
        or (p.is_file() and (p.suffix in FORBIDDEN_SUFFIXES
                             or p.name in FORBIDDEN_NAMES
                             or p.name.endswith(".egg-info")))
    )
    assert not found, (
        f"{len(found)} build/runtime path(s) in the release twin — it carries "
        f"source and nothing else. Remove them before syncing or committing:\n  "
        + "\n  ".join(found[:25])
        + ("\n  …" if len(found) > 25 else "")
    )


def test_a_stale_studio_bundle_would_be_trusted_forever():
    """The specific one, named on its own so the failure explains itself.

    Kept separate from the sweep above because the consequence is different: the
    others are weight, this one changes what a client actually runs.
    """
    assert not (MIRROR / "studio" / "dist").exists(), (
        "Velaris/studio/dist exists. The studio unit runs "
        "`ExecStartPre=test -d dist || npm run build`, so a bundle that ships "
        "with the code is trusted forever and the client never builds their own."
    )


def test_the_mirrors_own_files_are_still_there():
    """The other half of the rule: this guard must never tempt anyone into
    'cleaning' the four things the mirror owns and helix has no copy of."""
    for name in (".env", "git_push.sh", ".gitignore", "fonts"):
        assert (MIRROR / name).exists(), (
            f"Velaris/{name} is missing. It is mirror-only — there is no source "
            f"for it in helix, so nothing can put it back."
        )
