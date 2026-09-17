"""One tenant per mailbox — who may see, use and change which email rows.

Added 2026-09-17. Until then every email route was tenant-blind: any holder of
`platform.email.*` — which is every `admin`, and on a shared install `admin` is a
*tenant* administrator — listed, edited and deleted every tenant's mailboxes
(SMTP/IMAP credentials included), read every tenant's mail, sent through any
tenant's mailbox, and polled all of them. A mailbox is a tenant's identity to the
outside world, so it belongs to exactly one tenant.

The rules, in one place so every route and background sender applies the same:

* **A superadmin sees every tenant.** That is the documented break-glass.
* **Everyone else sees their own tenant's rows** — matched by slug or id, because
  `email_accounts.tenant_id` and `email_messages.tenant_id` are strings and
  installs have written both. A caller with no tenant claim is the default
  tenant, never every tenant.
* **A row with NO tenant belongs to the default tenant.** Not to everybody: an
  unassigned mailbox is not shared across tenants. This is what keeps a
  single-tenant install — where no mailbox was ever given a tenant — working
  exactly as before.
* **The default outbound mailbox is chosen per tenant.** Marking one mailbox the
  default clears the flag only among that tenant's mailboxes.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.tenancy.identity import DEFAULT_TENANT, is_superadmin, resolve_tenant


@dataclass(frozen=True)
class MailScope:
    #: A superadmin: no clause at all.
    everything: bool
    #: Every spelling of the caller's tenant (slug and id).
    tokens: tuple[str, ...]
    #: Whether the caller is the default tenant, and therefore owns NULL rows.
    owns_unassigned: bool
    #: The spelling written onto a row this caller creates.
    slug: str | None


async def scope_for_token(session: AsyncSession, token: Any) -> MailScope:
    """The scope of a tenant token (slug, id, or None for the default tenant)."""
    key = str(token).strip() if token not in (None, "") else DEFAULT_TENANT
    resolved = await resolve_tenant(session, key)
    tokens = {key}
    slug = key
    if resolved is not None:
        tokens |= {str(resolved[0]), resolved[1]}
        slug = resolved[1]
    default = DEFAULT_TENANT in tokens
    return MailScope(everything=False, tokens=tuple(sorted(tokens)),
                     owns_unassigned=default, slug=slug)


async def scope_for(session: AsyncSession, user: Any) -> MailScope:
    if is_superadmin(user):
        return MailScope(everything=True, tokens=(), owns_unassigned=True, slug=None)
    return await scope_for_token(session, getattr(user, "tenant_id", None))


def clause(scope: MailScope, column):
    """A WHERE clause for any string tenant column under this scope, or None."""
    if scope.everything:
        return None
    mine = column.in_(scope.tokens)
    return or_(mine, column.is_(None)) if scope.owns_unassigned else mine


def owns(scope: MailScope, row_tenant: Any) -> bool:
    if scope.everything:
        return True
    if row_tenant in (None, ""):
        return scope.owns_unassigned
    return str(row_tenant) in scope.tokens


async def default_outbound(session: AsyncSession, scope: MailScope):
    """This tenant's active default outbound mailbox, or None.

    A mailbox of the tenant's own is preferred to an unassigned one, so a tenant
    that has set up its mailbox never sends through the legacy unassigned
    default. A superadmin with no tenant uses the default tenant's.
    """
    from velaris_shared.db.models import EmailAccountModel

    if scope.everything:
        scope = await scope_for_token(session, None)
    rows = (await session.execute(
        select(EmailAccountModel).where(
            EmailAccountModel.is_default_outbound.is_(True),
            EmailAccountModel.is_active.is_(True),
            clause(scope, EmailAccountModel.tenant_id),
        ))).scalars().all()
    own = [r for r in rows if r.tenant_id not in (None, "")]
    return (own or rows or [None])[0]
