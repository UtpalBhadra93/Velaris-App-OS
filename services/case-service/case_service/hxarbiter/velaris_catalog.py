"""HxArbiter — Velaris' attribute registrations.

The catalog machinery is `hxarbiter_core`; what a *deployment* can actually
resolve is not. Every path here must be answerable by a real PIP before a policy
is allowed to rely on it — a catalog that promises attributes nothing supplies is
how a shadow-mode engine produces confident nonsense.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from hxarbiter_core import AttrSpec, AttrType, AttributeCatalog

#: Velaris' registrations. Kept deliberately small for ARB-A: every path here
#: must be resolvable by a real PIP before a policy can rely on it, and a
#: catalog that promises attributes nothing supplies is how a shadow-mode
#: engine produces confident nonsense.
VELARIS_CATALOG = AttributeCatalog(
    [
        # -- tenant ----------------------------------------------------------
        # `region` is NOT a column on `tenants`; it resolves through HxGlobal
        # (design §6.1). The catalog says nothing about where it comes from,
        # which is the point — the PIP owns that.
        AttrSpec(path="tenant.region", type=AttrType.STRING,
                 description="Sovereign region assigned to the tenant, via HxGlobal."),
        AttrSpec(path="tenant.plan", type=AttrType.STRING,
                 description="Commercial plan."),
        AttrSpec(path="tenant.flags.local_only", type=AttrType.BOOLEAN,
                 description="Legacy HxNexus AI egress flag, bridged for parity."),

        # -- subject ---------------------------------------------------------
        AttrSpec(path="subject.kind", type=AttrType.STRING,
                 allowed=("user", "service", "app", "workflow", "agent"),
                 description="What kind of principal is asking."),
        AttrSpec(path="subject.id", type=AttrType.STRING, description="Principal id."),
        AttrSpec(path="subject.roles", type=AttrType.STRING_SET,
                 description="Roles held by the subject."),
        AttrSpec(path="subject.mfa_age_seconds", type=AttrType.NUMBER,
                 description="Seconds since the subject last completed MFA."),

        # -- agent (F-00) ----------------------------------------------------
        # Only ever populated when subject.kind == "agent". Both are supplied by
        # the caller that already holds them — the agent registry for the kind,
        # the chokepoint for the model — and NEITHER is resolvable by a PIP,
        # which is deliberate: an agent must not be able to answer a question
        # about itself that a policy uses to decide about it.
        # `agents.attribution.subject_attrs()` is the only writer, and it reads
        # the registry row and the backend that answered, not the token.
        AttrSpec(path="subject.agent_kind", type=AttrType.STRING,
                 allowed=("internal", "marketplace", "external"),
                 description="Which sort of agent principal is asking (F-00)."),
        AttrSpec(path="subject.model_id", type=AttrType.STRING,
                 description="model@version that actually answered, stamped per call."),

        # -- resource --------------------------------------------------------
        AttrSpec(path="resource.kind", type=AttrType.STRING, description="Resource type."),
        AttrSpec(path="resource.data_classes", type=AttrType.STRING_SET,
                 description="HxPrivacy classifications carried by the resource."),
        AttrSpec(path="resource.case_type", type=AttrType.STRING, description="Case type slug."),
        AttrSpec(path="resource.region", type=AttrType.STRING,
                 description="Region the resource's data is held in."),

        # -- consent ---------------------------------------------------------
        AttrSpec(path="consent.basis", type=AttrType.STRING,
                 allowed=("consent", "contract", "legal_obligation", "vital_interests",
                          "public_task", "legitimate_interests", "anonymized", "none"),
                 description="HxConsent lawful basis for the request's purpose."),

        # -- context ---------------------------------------------------------
        AttrSpec(path="context.provider", type=AttrType.STRING,
                 description="Named AI or integration provider."),
        AttrSpec(path="context.provider_class", type=AttrType.STRING,
                 allowed=("local", "external"),
                 description="Whether inference leaves the deployment."),
        AttrSpec(path="context.target_domain", type=AttrType.DOMAIN,
                 description="Outbound host for an egress decision."),
        AttrSpec(path="context.ip_country", type=AttrType.STRING,
                 description="Two-letter country of the calling address."),
        AttrSpec(path="context.purpose", type=AttrType.STRING,
                 description="Declared processing purpose."),
        AttrSpec(path="context.destination_region", type=AttrType.STRING,
                 description="Region the data would land in."),

        # -- registered for the ARB-B..D enforcement points -------------------
        # Every one of these is supplied by a PEP that actually sends it. A
        # catalog entry with no producer is worse than a missing one: a rule
        # written against it compiles, never matches, and looks active.
        AttrSpec(path="context.from_stage", type=AttrType.STRING,
                 description="Stage a case is leaving (case.stage.transition)."),
        AttrSpec(path="context.to_stage", type=AttrType.STRING,
                 description="Stage a case is entering (case.stage.transition)."),
        # 198 — supplied by the case action dispatcher, which is the only place
        # a status changes. Registered because the PEP genuinely sends them: a
        # catalog entry with no producer compiles, never matches, and looks
        # active, which is the failure this block warns about.
        AttrSpec(path="context.from_status", type=AttrType.STRING,
                 description="Status a case is leaving (case.status.transition)."),
        AttrSpec(path="context.to_status", type=AttrType.STRING,
                 description="Status a case is entering (case.status.transition)."),
        AttrSpec(path="context.to_status_category", type=AttrType.STRING,
                 description="open | working | waiting | terminal — lets a policy "
                             "reason about a case ENDING without knowing a "
                             "tenant's own status names."),
        AttrSpec(path="context.terminal", type=AttrType.BOOLEAN,
                 description="True when the target status ends the case."),
        AttrSpec(path="context.action", type=AttrType.STRING,
                 description="The case action being performed, from the closed "
                             "registry (case.status.transition)."),
    ]
)
