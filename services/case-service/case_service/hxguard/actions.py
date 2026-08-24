"""HxGuard action registry — Phase 1 of privilege-based authorization.

Every `require_role(...)` gate in the routers is named here as an **action**.
Actions are declared in code, never in data: data decides who *holds* an action,
code decides which actions *exist*. That is what keeps the model fail-closed —
a typo'd privilege in the database grants nothing rather than inventing a
capability.

`legacy_roles` records the roles the gate required before this migration. It is
not decoration: `privileges.SEEDED_ROLE_PRIVILEGES` is derived from it, and
`tests/phase_authz` asserts that the derived answer matches `require_role` for
every action and every subject shape. Delete a `legacy_roles` entry and the
parity proof for that action goes with it.

**Nothing here is enforced yet.** Phase 1 adds the registry and the evaluator
alongside the existing gates; Phase 2 shadows them; Phase 3 cuts over.

Source: docs/Future/Done/authz-gate-inventory.md (Phase 0, 2026-08-13).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ActionDef:
    """One authorization decision the platform can be asked to make."""

    #: `<domain>.<resource>.<verb>` — the verb is the last dot-segment.
    name: str
    #: "endpoint" gates one route; "module" gates a whole router. A module
    #: action cannot be split into read/write because a router-level dependency
    #: fires before the handler is known — that split is Phase 3 work.
    kind: Literal["endpoint", "module"]
    #: Roles the pre-migration `require_role(...)` gate required. The parity
    #: oracle, not documentation.
    legacy_roles: tuple[str, ...]
    #: What the action actually protects, in one sentence.
    summary: str


def _a(name, kind, roles, summary):
    return ActionDef(name=name, kind=kind, legacy_roles=tuple(roles), summary=summary)


#: Every action the platform evaluates. Unknown action ⇒ deny, always.
ACTIONS: dict[str, ActionDef] = {}


def _reg(a: ActionDef) -> None:
    if a.name in ACTIONS:
        raise ValueError(f"duplicate action {a.name!r}")
    ACTIONS[a.name] = a


# ── endpoint actions ──────────────────────────────────────────────
_reg(_a("ai.metrics.read", "endpoint", ['admin'],
        "Per-call AI cost and volume. Commercially sensitive."))
_reg(_a("compliance.audit.seal", "endpoint", ['admin'],
        "Anchors the chain externally."))
_reg(_a("compliance.consent.read", "endpoint", ['admin', 'security'],
        "Is this processing lawful right now."))
_reg(_a("compliance.consent.write", "endpoint", ['admin', 'security'],
        "Appends to the lawful-basis ledger. Append-only by design."))
_reg(_a("compliance.dr.read", "endpoint", ['admin'],
        "RPO/RTO posture per region."))
_reg(_a("compliance.dr.write", "endpoint", ['admin'],
        "An untested backup is a hope — this is the evidence."))
_reg(_a("compliance.ledger.read", "endpoint", ['admin'],
        "Payments vs ledger. Reports, never corrects."))
_reg(_a("compliance.ledger.write", "endpoint", ['admin'],
        "The only correction mechanism — a mirror posting."))
_reg(_a("compliance.privacy.read", "endpoint", ['admin', 'security'],
        "PII findings and their review state."))
_reg(_a("compliance.privacy.review", "endpoint", ['admin', 'security'],
        "A human verdict a rescan must never overwrite."))
_reg(_a("compliance.privacy.scan", "endpoint", ['admin', 'security'],
        "Scans every form and data model for personal data."))
_reg(_a("compliance.reports.generate", "endpoint", ['admin'],
        "Produces a compliance report."))
_reg(_a("bots.definitions.read", "endpoint", ['admin', 'developer'],
        "The bot registry."))
_reg(_a("bots.definitions.write", "endpoint", ['admin', 'developer'],
        "Registers a container image that will run on this host."))
_reg(_a("bots.definitions.approve", "endpoint", ['admin'],
        "Puts a bot into production. The review gate — never the author."))
_reg(_a("bots.credentials.write", "endpoint", ['admin'],
        "Stores a third party's password for a bot to sign in with."))
_reg(_a("bots.jobs.read", "endpoint", ['admin', 'developer', 'manager', 'case_worker'],
        "Job status and the attended proposal diff."))
_reg(_a("bots.jobs.run", "endpoint", ['admin', 'developer', 'manager', 'case_worker'],
        "Runs a bot, cancels one, or accepts its proposed changes."))
_reg(_a("channels.endpoints.read", "endpoint", ['admin'],
        "Channel endpoints — the numbers customers text."))
_reg(_a("channels.endpoints.write", "endpoint", ['admin'],
        "Creates an inbound webhook and its signing secret. A wrong secret "
        "here means an unauthenticated case-creation endpoint."))
_reg(_a("channels.conversations.read", "endpoint", ['admin', 'manager', 'case_worker'],
        "The channel inbox and one thread's messages."))
_reg(_a("channels.conversations.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Replying, assigning and closing. Replies cost money and reach a "
        "customer's phone."))
_reg(_a("channels.cti.pop", "endpoint", ['admin', 'manager', 'case_worker'],
        "Screen-pop from a contact-centre platform."))
_reg(_a("compliance.retention.run", "endpoint", ['admin'],
        "Deletes data. The most destructive action in this table."))
_reg(_a("docs.guides.regenerate", "endpoint", ['designer', 'admin'],
        "Regenerates the generated guides."))
_reg(_a("identity.directory.read", "endpoint", ['admin'],
        "The people directory."))
_reg(_a("identity.directory.sync", "endpoint", ['admin'],
        "The SSO/LDAP upsert path. SCIM will land here (sequence item 4)."))
_reg(_a("identity.directory.write", "endpoint", ['admin'],
        "Deactivation, not deletion."))
_reg(_a("identity.groups.read", "endpoint", ['admin'],
        "Membership."))
_reg(_a("identity.groups.write", "endpoint", ['admin'],
        "Lockout risk — can remove the last member who can grant access."))
_reg(_a("identity.permissions.write", "endpoint", ['admin'],
        "The Studio route-visibility map. UI only — never a backend gate."))
_reg(_a("identity.portals.read", "endpoint", ['admin'],
        "One module set."))
_reg(_a("identity.portals.write", "endpoint", ['admin'],
        "Removes a module set out from under its groups."))
_reg(_a("identity.provisioning.read", "endpoint", ['admin'],
        "IdP group mappings and the provisioning log — why a user is in a group."))
_reg(_a("identity.provisioning.write", "endpoint", ['admin'],
        "Maps an IdP group onto an access group. This is how a directory "
        "confers authorization here, so it is the escalation path to watch."))
_reg(_a("identity.scim.read", "endpoint", ['admin'],
        "SCIM token listing. Metadata only — the token itself is unrecoverable."))
_reg(_a("identity.scim.write", "endpoint", ['admin'],
        "Mints and revokes SCIM tokens. A minted token provisions and "
        "deprovisions accounts for its whole tenant."))
_reg(_a("identity.roles.read", "endpoint", ['admin'],
        "One role's privileges."))
_reg(_a("identity.roles.write", "endpoint", ['admin'],
        "Revokes a bundle from everyone holding it."))
_reg(_a("identity.sso.configure", "endpoint", ['admin'],
        "Removes a sign-in path for everyone using it."))
_reg(_a("identity.sso.read", "endpoint", ['admin'],
        "Provider config incl. allowed domains; never the secret."))
_reg(_a("identity.tokens.configure", "endpoint", ['admin'],
        "Lengthens or shortens every session on the platform."))
_reg(_a("identity.tokens.read", "endpoint", ['admin'],
        "Session lifetime setting."))
_reg(_a("identity.users.read", "endpoint", ['admin'],
        "One account. Was ungated until 2026-08-13."))
_reg(_a("identity.users.write", "endpoint", ['admin'],
        "Removes a login."))
_reg(_a("platform.calendars.write", "endpoint", ['admin'],
        "Same, for an existing calendar."))
_reg(_a("platform.email.configure", "endpoint", ['admin'],
        "Removes the account."))
_reg(_a("platform.email.operate", "endpoint", ['admin'],
        "Live SMTP/IMAP probe using stored credentials."))
_reg(_a("platform.push.configure", "endpoint", ['admin'],
        "Changes who gets notified about what."))
_reg(_a("platform.push.operate", "endpoint", ['admin'],
        "Sends a real notification to a real device."))
_reg(_a("platform.push.read", "endpoint", ['admin'],
        "Per-case-type notification overrides."))
_reg(_a("platform.status.read", "endpoint", ['admin'],
        "Diagnostic detail — deliberately not the public status."))
_reg(_a("platform.updates.approve", "endpoint", ['admin'],
        "Per-ring approval in per-env mode."))
_reg(_a("platform.updates.approve_prod", "endpoint", ['admin'],
        "Second deliberate gate — the prod ring. Keep it separable from `approve`."))
_reg(_a("platform.updates.configure", "endpoint", ['admin'],
        "Changes how updates are approved at all."))
_reg(_a("platform.updates.read", "endpoint", ['admin'],
        "Every registered environment's version."))
_reg(_a("platform.updates.request", "endpoint", ['admin'],
        "Withdraws that approval."))
_reg(_a("platform.updates.rollback", "endpoint", ['admin'],
        "Reverts this environment."))
_reg(_a("portal.config.read", "endpoint", ['admin'],
        "Public presentation of a case type."))
_reg(_a("portal.config.write", "endpoint", ['admin'],
        "Customer-visible branding asset."))
_reg(_a("portal.customers.erase", "endpoint", ['admin'],
        "GDPR Art. 17 erasure — irreversible. Deserves its own action."))
_reg(_a("portal.customers.read", "endpoint", ['admin'],
        "One customer's record."))
_reg(_a("portal.customers.write", "endpoint", ['admin'],
        "Create a customer account on someone's behalf — the only path to an "
        "account once self-registration is switched off."))
_reg(_a("portal.submissions.read", "endpoint", ['admin'],
        "What customers submitted."))
_reg(_a("variables.definitions.write", "endpoint", ['designer'],
        "Removes a definition."))


# ── Phase 3.5: the gates Phase 0 could not see ────────────────────
# `require_admin()` is a one-line alias for `require_role("admin")`, so the
# Phase-0 grep for `require_role(` walked straight past 56 gates in four
# routers. Same defect class as the router-level `Depends(` gates that grep
# also hid — an alias is a gate, and a gate you cannot count is a gate you
# cannot migrate.
#
# All 56 were bare `admin`, so every action here seeds to admin-only and the
# cutover is behaviour-preserving. The read/write split is the same win Phase 1
# delivered elsewhere: 24 of the 56 are read-only and demanded the same `admin`
# as the writes beside them, so nobody could look at a storefront's catalogue
# without also being able to rewrite it.
_reg(_a("commerce.analytics.read", "endpoint", ['admin'],
        "Checkout revenue and conversion totals. Commercially sensitive."))
_reg(_a("commerce.content.read", "endpoint", ['admin'],
        "Storefront theme, pages, navigation, SEO and media."))
_reg(_a("commerce.content.write", "endpoint", ['admin'],
        "Publishes customer-facing storefront content."))
_reg(_a("commerce.integrations.read", "endpoint", ['admin'],
        "Which payment/fulfilment integrations are configured."))
_reg(_a("commerce.integrations.write", "endpoint", ['admin'],
        "Configures an integration — holds third-party credentials."))
_reg(_a("commerce.inventory.read", "endpoint", ['admin'],
        "Stock levels and their adjustment history."))
_reg(_a("commerce.inventory.write", "endpoint", ['admin'],
        "Adjusts stock. Moves real, sellable quantity."))
_reg(_a("commerce.orders.read", "endpoint", ['admin'],
        "Customer orders, including buyer detail."))
_reg(_a("commerce.products.read", "endpoint", ['admin'],
        "Catalogue: products, variants, options, categories."))
_reg(_a("commerce.products.write", "endpoint", ['admin'],
        "Changes what is for sale and how it is grouped."))
_reg(_a("commerce.promotions.read", "endpoint", ['admin'],
        "Promotions, and validating a code against a cart."))
_reg(_a("commerce.promotions.write", "endpoint", ['admin'],
        "Creates or withdraws a discount — directly affects revenue."))
_reg(_a("commerce.stores.read", "endpoint", ['admin'],
        "Store list and configuration."))
_reg(_a("commerce.stores.write", "endpoint", ['admin'],
        "Creates, reconfigures, clones or archives a storefront."))
_reg(_a("commerce.tokens.read", "endpoint", ['admin'],
        "Which checkout service tokens exist."))
_reg(_a("commerce.tokens.write", "endpoint", ['admin'],
        "Mints or revokes a checkout service token — a credential."))
_reg(_a("platform.hxtest.write", "endpoint", ['admin'],
        "Generates and registers HxTest suites."))
_reg(_a("platform.testsuite.read", "endpoint", ['admin'],
        "Past test runs and their streamed output."))
_reg(_a("platform.testsuite.run", "endpoint", ['admin'],
        "Executes a suite against the running platform."))


# ── module actions (router-level gates) ───────────────────────────
# Each of these governs an entire router. They are the most consequential gates
# in the codebase: a router dependency applies to every route added to that file
# afterwards, so migrating a module endpoint-by-endpoint and missing the router
# line would remove authorization from all of it.
_reg(_a("analytics.mining.access", "module", ['designer', 'admin'],
        "Whole-router gate on process_mining.py (~8 routes)."))
_reg(_a("compliance.enterprise.access", "module", ['admin'],
        "Whole-router gate on enterprise.py (~15 routes)."))
_reg(_a("design.codegen.access", "module", ['designer', 'admin'],
        "Whole-router gate on codegen.py (~3 routes)."))
_reg(_a("design.data_models.access", "module", ['designer', 'admin'],
        "Whole-router gate on data_models.py (~5 routes)."))
_reg(_a("design.nlp.access", "module", ['designer', 'admin'],
        "Whole-router gate on nlp.py (~4 routes)."))
_reg(_a("integration.fusion.access", "module", ['integration', 'admin', 'designer'],
        "Whole-router gate on hxfusion.py (~15 routes)."))
_reg(_a("platform.admin.access", "module", ['admin'],
        "Whole-router gate on admin.py (~9 routes)."))
_reg(_a("platform.orchestrator.access", "module", ['devops', 'admin'],
        "Whole-router gate on orchestrator.py (~12 routes)."))
_reg(_a("platform.scout.access", "module", ['devops', 'admin'],
        "Whole-router gate on scout.py (~7 routes)."))
_reg(_a("platform.scout_ai.access", "module", ['devops', 'admin'],
        "Whole-router gate on scout_ai.py (~6 routes)."))
_reg(_a("platform.tenants.access", "module", ['admin'],
        "Whole-router gate on tenants.py (~12 routes)."))
_reg(_a("security.shield.access", "module", ['security', 'admin'],
        "Whole-router gate on hxshield.py (~12 routes)."))


# ── Phase 4 step 2: the imperative checks ─────────────────────────
# Not gates at all, in the FastAPI sense — role checks written as statements in
# handler bodies, or in sync helpers those handlers call. 205 sites across 24
# routers, of which 45 are roots (the rest inherit by calling a root).
#
# They differ from every earlier wave in one way that shapes the naming below:
# a dependency either passes or raises, but a statement in a body can also
# *shape a response* — scope a query, redact a column, hide a field. Those need
# an action just as much, and they are the ones a blanket "make it admin-only"
# rewrite would have broken, because their answer is a `bool`, not a refusal.
#
# `legacy_roles` continues to mean "what the pre-migration check required", so
# the seeding stays derived. Where a check tested `has_privilege("*", "*")` as
# well, that is NOT reproduced as a role: the legacy access-group wildcard is
# deliberately outside the platform namespace (see privileges.py), and Phase 4
# step 1 is what made that boundary load-bearing.

# Module-level access, previously `_require_admin`-style statements.
_reg(_a("platform.dbmigrate.access", "module", ['admin', 'superadmin'],
        "HxDBMigrate — moves data between live databases (~18 routes)."))
_reg(_a("design.evolve.access", "module", ['admin', 'superadmin'],
        "HxEvolve insight review and proposal decisions (~9 routes)."))
_reg(_a("platform.replay.access", "module", ['admin', 'superadmin'],
        "HxReplay — replay runs across other operators' cases (~6 routes)."))
_reg(_a("platform.stream.access", "module", ['admin'],
        "HxStream event ingest and query; staff-level portal or above."))
_reg(_a("platform.stream.read_all", "endpoint", ['admin'],
        "Read every actor's stream events, not just your own."))
_reg(_a("platform.dbmanager.access", "module", ['admin'],
        "HxDBManager — direct database browsing (~17 routes)."))
_reg(_a("platform.dbmanager.read", "endpoint", ['admin', 'designer', 'developer'],
        "Schema browser and table viewer; the developer tier sees only _DEV_TABLES."))
_reg(_a("platform.dbmanager.reauth", "endpoint", ['admin', 'designer', 'developer'],
        "Step-up re-authentication for the DB viewer session."))

# Marketplace — package distribution, distinct from the `commerce.*` storefront.
# `marketplace.admin.access` covers the 33 routes behind `_require_admin`
# (sources, capability grants, the review queue, installs, blacklist, access
# rules, release requests). They were one bare `admin` check, so they stay one
# action: inventing a finer split here would be a guess at a boundary the code
# has never drawn, and a guess is what the parity discipline exists to avoid.
_reg(_a("marketplace.admin.access", "module", ['admin'],
        "Marketplace administration: sources, grants, review queue, installs, blacklist."))
_reg(_a("marketplace.packages.publish", "endpoint", ['admin', 'developer'],
        "Build and submit a package others can install."))
_reg(_a("marketplace.catalogue.read", "endpoint", ['admin', 'developer', 'manager'],
        "Reach the marketplace at all."))
_reg(_a("marketplace.workspaces.override", "endpoint", ['admin'],
        "Act on a workspace you did not create, and bypass the per-user limit."))

# Design-time authoring, previously ownership-plus-admin-override statements.
_reg(_a("design.branches.override", "endpoint", ['admin'],
        "Act on a branch you do not own — review, merge, recall, revert."))
_reg(_a("design.case_types.write", "endpoint", ['admin', 'designer'],
        "Create or modify a case type."))
_reg(_a("design.draft.apply", "endpoint", ['admin', 'designer'],
        "Apply an HxDraft proposal to live configuration."))
_reg(_a("design.rules.read", "endpoint", ['admin', 'designer'],
        "Rule-migration reporting across case types."))

# Response-shaping actions: these answer `bool`, and the answer redacts or
# scopes rather than refuses.
_reg(_a("finance.details.read", "endpoint", ['admin', 'finance'],
        "Unredacted invoice and payment financials; otherwise totals-only."))
_reg(_a("casework.sensitive.read", "endpoint", ['admin'],
        "Cases and fields marked sensitive; otherwise filtered from the list."))
_reg(_a("docs.files.delete", "endpoint", ['admin'],
        "Delete a document or one of its versions."))
_reg(_a("integration.connectors.rotate", "endpoint", ['admin', 'integration'],
        "Rotate stored connector credentials, and apply a drafted connector change."))
_reg(_a("variables.operator.elevated", "endpoint", ['admin'],
        "Operator context that bypasses case-variable visibility scoping."))
_reg(_a("compliance.crosstenant.read", "endpoint", ['admin'],
        "See PII findings and backup records beyond your own tenant."))

# ── 2026-08-15 verification: routes that had NO gate at all ───────
# Every phase so far migrated the gates that EXISTED. Nothing ever inventoried
# the routes that had none — a `require_role` grep cannot see an absent gate, and
# neither could any of the three detectors that followed it. A post-Phase-4 sweep
# of the BUILT app (not the source) found 58 such routes, 29 of them writes.
# These actions close the ones with no legitimate anonymous caller.
_reg(_a("platform.email.read", "endpoint", ['admin'],
        "Mailbox contents, templates and account config. Message bodies are correspondence."))
_reg(_a("analytics.reports.read", "endpoint", ['admin', 'manager'],
        "Case volumes, SLA compliance and stage bottlenecks across the deployment."))
_reg(_a("platform.intake.read", "endpoint", ['admin'],
        "Raw inbound intake events, before they become cases."))
_reg(_a("design.escalation.read", "endpoint", ['admin', 'manager'],
        "Escalation trees and a case's escalation schedule."))
_reg(_a("design.escalation.write", "endpoint", ['admin'],
        "Create, edit or delete an escalation tree; fire an escalation."))
_reg(_a("compliance.frameworks.read", "endpoint", ['admin', 'security'],
        "Which compliance frameworks this deployment claims."))


# -- Phase 5 (2026-08-15): the case layer's own role registry ------
# `RbacBackend.ACTION_ROLES` was the LAST role-name decision table in the
# platform - Phase A built it to mirror `require_role`, and phases 1-4 migrated
# everything around it without touching it. Same shape as Phase 0's inventory
# (action -> role set, with an `is_admin` bypass), so the same treatment applies
# and `legacy_roles` is again transcribed straight from the table it replaces.
#
# `case.rule.apply` is deliberately NOT here. Its role set is EMPTY, meaning
# "any authenticated user" - a privilege model cannot express that (a subject
# with no roles holds no privileges), so registering it would silently tighten
# an open surface. It stays in `ACTION_ROLES`, which is now what that dict is
# for: decisions that are about authentication, not about privilege.
_reg(_a("connector.namespace.register", "endpoint", ['admin', 'integration'],
        "Bind a connector to a variable namespace."))
_reg(_a("costing.rates.access", "endpoint", ['admin'],
        "Rate cards. Commercially sensitive."))
_reg(_a("devconn.namespace.register", "endpoint", ['admin', 'developer', 'integration'],
        "Bind a DevConn build to a variable namespace."))
_reg(_a("mcp.tokens.mint", "endpoint", ['admin', 'designer'],
        "Mint a scoped external-agent (MCP) token."))
_reg(_a("replay.cohorts.run", "endpoint", ['admin'],
        "HxReplay cohort replay: bulk read across other operators' cases."))
_reg(_a("risk.outcomes.write", "endpoint", ['admin', 'manager'],
        "Write ground truth. A wrong label poisons everything learned later."))
_reg(_a("risk.assessment.read", "endpoint", ['admin', 'case_worker', 'manager'],
        "HxSentinel findings and outcome labels."))
_reg(_a("risk.rules.configure", "endpoint", ['admin', 'manager'],
        "Tune the HxSentinel rule engine - tenant-wide."))
_reg(_a("risk.scores.review", "endpoint", ['admin', 'manager'],
        "Supervise reviewers' scores; dashboard and dispute."))
_reg(_a("risk.taxonomy.write", "endpoint", ['admin'],
        "Outcome vocabulary - tenant-wide."))
_reg(_a("rules.definitions.write", "endpoint", ['admin', 'designer'],
        "Rule definition CRUD - gates automation behaviour."))
_reg(_a("variables.namespace.admin", "endpoint", ['admin'],
        "Create or administer a case-variable namespace."))
_reg(_a("variables.blobkeys.promote", "endpoint", ['admin'],
        "Promote a blob key to a typed variable, platform-wide for that type."))
_reg(_a("variables.blobkeys.scan", "endpoint", ['admin', 'designer'],
        "Scan a case type's blob keys for promotable variables."))

# The ReBAC bypass. `RebacBackend` answered `superadmin or is_admin or manager`
# before it looked at a single relationship, so "manager sees every case" was
# unrevocable and invisible - it appeared in no gate, no registry and no UI.
# Naming it makes it a privilege like any other; seeding it to admin+manager
# keeps today's answer exactly.
_reg(_a("casework.records.any", "endpoint", ['admin', 'manager'],
        "Act on any case without a relationship to it. The ReBAC bypass."))


#: Every role name that appears in any gate. A role outside this set grants no
#: platform action today, which is why `viewer`/`manager`/`case_worker` seed to
#: an empty privilege set rather than to something plausible-looking.
GATE_ROLES: frozenset[str] = frozenset(
    r for a in ACTIONS.values() for r in a.legacy_roles
)




#: Which router handler each endpoint action came from, as `file.py::function`.
#: Phase 2 resolves a live route back to its action through this index; Phase 3
#: deletes the gate it names. A key here with no matching handler means a route
#: was renamed and its shadow comparison silently stopped running.
ACTION_GATES: dict[str, tuple[str, ...]] = {
    # Phase 3.5 — the `require_admin()` gates.
    "commerce.analytics.read": ('checkout.py::analytics_summary',),
    "commerce.content.read": ('storefront.py::get_navigation', 'storefront.py::get_page', 'storefront.py::get_seo', 'storefront.py::get_theme', 'storefront.py::list_media', 'storefront.py::list_pages'),
    "commerce.content.write": ('storefront.py::delete_media', 'storefront.py::preview_theme', 'storefront.py::save_navigation', 'storefront.py::save_page', 'storefront.py::save_seo', 'storefront.py::save_theme', 'storefront.py::upload_media'),
    "commerce.integrations.read": ('checkout.py::list_integrations',),
    "commerce.integrations.write": ('checkout.py::create_integration', 'checkout.py::delete_integration', 'checkout.py::update_integration'),
    "commerce.inventory.read": ('storefront.py::inventory_history', 'storefront.py::list_inventory'),
    "commerce.inventory.write": ('storefront.py::adjust_inventory',),
    "commerce.orders.read": ('checkout.py::list_orders',),
    "commerce.products.read": ('storefront.py::get_product', 'storefront.py::list_categories', 'storefront.py::list_products'),
    "commerce.products.write": ('storefront.py::add_option', 'storefront.py::add_variant', 'storefront.py::archive_product', 'storefront.py::create_category', 'storefront.py::create_product', 'storefront.py::delete_category', 'storefront.py::delete_variant', 'storefront.py::set_product_categories', 'storefront.py::update_product'),
    "commerce.promotions.read": ('storefront.py::list_promotions', 'storefront.py::validate_promotion_admin'),
    "commerce.promotions.write": ('storefront.py::create_promotion', 'storefront.py::deactivate_promotion', 'storefront.py::update_promotion'),
    "commerce.stores.read": ('storefront.py::get_store', 'storefront.py::list_stores'),
    "commerce.stores.write": ('storefront.py::archive_store', 'storefront.py::clone_store', 'storefront.py::create_store', 'storefront.py::update_store'),
    "commerce.tokens.read": ('checkout.py::list_tokens',),
    "commerce.tokens.write": ('checkout.py::create_token', 'checkout.py::revoke_token'),
    "platform.hxtest.write": ('hxtest.py::generate', 'hxtest.py::register_bundled'),
    "platform.testsuite.read": ('testsuite.py::list_runs', 'testsuite.py::run_detail', 'testsuite.py::run_stream'),
    "platform.testsuite.run": ('testsuite.py::generate_suite', 'testsuite.py::run', 'testsuite.py::run_conformance'),

    "ai.metrics.read": ('hxnexus.py::llmops_metrics',),
    "compliance.audit.seal": ('compliance.py::seal_chain', 'compliance.py::trigger_audit_anchor'),
    "compliance.consent.read": ('compliance.py::check_consent', 'compliance.py::get_consent_state'),
    "compliance.consent.write": ('compliance.py::record_consent',),
    "compliance.dr.read": ('compliance.py::get_dr_posture', 'compliance.py::list_backups'),
    "compliance.dr.write": ('compliance.py::record_backup', 'compliance.py::record_restore_test'),
    "compliance.ledger.read": ('compliance.py::get_trial_balance', 'compliance.py::reconcile_ledger'),
    "compliance.ledger.write": ('compliance.py::create_ledger_account', 'compliance.py::post_ledger_transaction', 'compliance.py::reverse_ledger_transaction'),
    "compliance.privacy.read": ('compliance.py::list_pii_findings',),
    "compliance.privacy.review": ('compliance.py::review_pii_finding',),
    "compliance.privacy.scan": ('compliance.py::run_pii_scan',),
    "compliance.reports.generate": ('compliance.py::generate_report',),
    "compliance.retention.run": ('enterprise.py::run_retention',),
    "docs.guides.regenerate": ('hxnexus.py::regenerate_docs',),
    "identity.directory.read": ('user_directory.py::list_entries',),
    "identity.directory.sync": ('user_directory.py::bulk_sync', 'user_directory.py::sync_from_db'),
    "identity.directory.write": ('user_directory.py::create_entry', 'user_directory.py::deactivate_entry', 'user_directory.py::update_entry'),
    "identity.groups.read": ('access_groups.py::get_access_group', 'access_groups.py::list_access_groups', 'access_groups.py::list_members'),
    "identity.groups.write": ('access_groups.py::add_member', 'access_groups.py::create_access_group', 'access_groups.py::delete_access_group', 'access_groups.py::remove_member', 'access_groups.py::update_access_group'),
    "identity.permissions.write": ('permissions.py::update_permissions',),
    "identity.portals.read": ('access_groups.py::get_portal', 'access_groups.py::list_portals'),
    "identity.portals.write": ('access_groups.py::create_portal', 'access_groups.py::delete_portal', 'access_groups.py::update_portal'),
    "identity.roles.read": ('access_groups.py::get_access_role', 'access_groups.py::get_privilege_catalog', 'access_groups.py::list_access_roles'),
    "identity.roles.write": ('access_groups.py::create_access_role', 'access_groups.py::delete_access_role', 'access_groups.py::update_access_role'),
    "identity.sso.configure": ('auth_real.py::create_sso_provider', 'auth_real.py::delete_sso_provider', 'auth_real.py::update_sso_provider'),
    "identity.sso.read": ('auth_real.py::manage_sso_providers',),
    "identity.tokens.configure": ('auth_real.py::set_token_expiry',),
    "identity.tokens.read": ('auth_real.py::get_token_expiry',),
    "identity.users.read": ('auth_real.py::get_user', 'auth_real.py::list_users'),
    "identity.users.write": ('auth_real.py::delete_user', 'auth_real.py::register_user', 'auth_real.py::update_user'),
    "platform.calendars.write": ('admin.py::create_calendar', 'admin.py::update_calendar'),
    "platform.email.configure": ('email.py::create_account', 'email.py::deactivate_account', 'email.py::delete_account', 'email.py::update_account'),
    "platform.email.operate": ('email.py::poll_now', 'email.py::test_connection'),
    "platform.push.configure": ('push.py::upsert_ct_override',),
    "platform.push.operate": ('push.py::test_send',),
    "platform.push.read": ('push.py::admin_delivery_logs', 'push.py::admin_list_devices', 'push.py::get_ct_overrides'),
    "platform.status.read": ('status.py::authz_parity', 'status.py::internal_status'),
    "platform.updates.approve": ('platform_updates.py::approve_plan', 'platform_updates.py::approve_run', 'platform_updates.py::halt_plan'),
    "platform.updates.approve_prod": ('platform_updates.py::approve_prod',),
    "platform.updates.configure": ('platform_updates.py::put_update_settings',),
    "platform.updates.read": ('platform_updates.py::fleet_versions', 'platform_updates.py::get_update_settings', 'platform_updates.py::list_plans', 'platform_updates.py::update_status'),
    "platform.updates.request": ('platform_updates.py::cancel_update_request', 'platform_updates.py::request_update'),
    "platform.updates.rollback": ('platform_updates.py::request_rollback',),
    "portal.config.read": ('portal.py::get_case_type_portal_display', 'portal.py::list_portal_tenants'),
    "portal.config.write": ('portal.py::toggle_case_type_portal', 'portal.py::update_case_type_portal_display', 'portal.py::update_portal_settings', 'portal.py::upload_portal_logo'),
    "portal.customers.erase": ('portal_customers.py::admin_delete_customer',),
    "portal.customers.read": ('portal_customers.py::admin_get_customer', 'portal_customers.py::admin_list_customers'),
    "portal.customers.write": ('portal_customers.py::admin_create_customer',),
    "portal.submissions.read": ('portal.py::list_portal_submissions',),
    "variables.definitions.write": ('case_variables.py::define_variable', 'case_variables.py::delete_variable', 'case_variables.py::patch_variable'),
}

#: Router file → module action, for the 12 router-level gates.
MODULE_ACTION_BY_FILE: dict[str, str] = {
    "process_mining.py": "analytics.mining.access",
    "enterprise.py": "compliance.enterprise.access",
    "codegen.py": "design.codegen.access",
    "data_models.py": "design.data_models.access",
    "nlp.py": "design.nlp.access",
    "hxfusion.py": "integration.fusion.access",
    "admin.py": "platform.admin.access",
    "orchestrator.py": "platform.orchestrator.access",
    "scout.py": "platform.scout.access",
    "scout_ai.py": "platform.scout_ai.access",
    "tenants.py": "platform.tenants.access",
    "hxshield.py": "security.shield.access",
}

#: Routers cut over to `hxguard.guard(...)`. **Phase 3 completed 2026-08-13 —
#: this is now every router that ever had a role gate, and `require_role` has no
#: remaining call sites in the API layer.** The ledger stays because it is what
#: `tests/phase_authz` checks against: a router here must have zero
#: `require_role` calls, and a gate reappearing anywhere is a regression rather
#: than a preference.
MIGRATED_ROUTERS: frozenset[str] = frozenset({
    "access_groups.py",
    "admin.py",
    "auth_real.py",
    "case_variables.py",
    "checkout.py",             # Phase 3.5
    "codegen.py",
    "compliance.py",
    "data_models.py",
    "email.py",
    "enterprise.py",
    "hxfusion.py",
    "hxnexus.py",
    "hxshield.py",
    "hxtest.py",               # Phase 3.5
    "nlp.py",
    "orchestrator.py",
    "permissions.py",
    "platform_updates.py",
    "portal.py",
    "portal_customers.py",
    "process_mining.py",
    "push.py",
    "scout.py",
    "scout_ai.py",
    "status.py",
    "storefront.py",           # Phase 3.5
    "tenants.py",
    "testsuite.py",            # Phase 3.5
    "user_directory.py",
})


#: Reverse index: `file.py::function` → endpoint action.
ACTION_BY_GATE: dict[str, str] = {
    gate: action for action, gates in ACTION_GATES.items() for gate in gates
}


def resource_of(action: str) -> str:
    """`compliance.ledger.read` → `compliance.ledger`."""
    return action.rsplit(".", 1)[0]


def verb_of(action: str) -> str:
    """`compliance.ledger.read` → `read`."""
    return action.rsplit(".", 1)[-1]
