/* ═══════════════════════════════════════════════════════════════════
   Admin Console — Enterprise Identity (SAML + SCIM)

   Four things an administrator has to do once a customer's IdP is involved,
   and each of them is here because doing it any other way is worse:

   1. **Hand the IdP team a metadata URL.** They will ask for one; typing our
      entity ID and ACS URL into their console by hand is how a Destination
      mismatch gets diagnosed three days later.
   2. **Mint a SCIM token.** The plaintext exists exactly once, in the response
      to the mint call — the server stores a SHA-256 and genuinely cannot show
      it again. The UI says so at the moment it matters rather than in a doc.
   3. **Map IdP groups onto access groups.** An unmapped group grants nothing,
      deliberately: there is no name-matching fallback, because the IdP admin
      owns those strings. So this grid is the whole of how a directory confers
      authorization here — and the "unmapped" list next to it is what turns
      "SSO works but nobody can see anything" into a five-second answer.
   4. **Paste an assertion and see the mapping.** The tester runs the real
      verifier, issues nothing and consumes nothing, so the same capture can be
      pasted repeatedly while an attribute map is fixed.
   5. **Link an account that existed before SSO.** A login will not adopt an
      account it matches only by email address, because an assertion naming an
      address is not proof of owning the account that holds it. Somebody with
      the privilege says so here instead — otherwise that refusal is a dead end
      for every customer whose staff already had accounts.

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useCallback, useEffect, useState } from "react";

import {
  createGroupMapping,
  createScimToken,
  deleteGroupMapping,
  linkAccount,
  listAccessGroupsBrief,
  listAccountLinks,
  listGroupMappings,
  listProvisionLog,
  listScimTokens,
  listUnmappedGroups,
  revokeScimToken,
  testSamlAssertion,
  unlinkAccount,
  type AccountLink,
  type IdentityGroupMapping,
  type ProvisionLogEntry,
  type ScimToken,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

const SOURCES = ["saml_jit", "scim", "oidc", "bulk_sync"] as const;

const SOURCE_LABELS: Record<string, string> = {
  saml_jit: "SAML (just-in-time)",
  scim: "SCIM",
  oidc: "OIDC",
  bulk_sync: "Bulk sync",
};

const cellStyle: React.CSSProperties = {
  padding: "var(--space-sm)", borderBottom: "1px solid var(--border)",
  textAlign: "left", verticalAlign: "top",
};

function Section({ title, subtitle, children }: {
  title: string; subtitle?: string; children: React.ReactNode;
}) {
  return (
    <Card style={{ marginBottom: "var(--space-xl)" }}>
      <h3 style={{ margin: 0 }}>{title}</h3>
      {subtitle && (
        <p style={{ margin: "var(--space-xs) 0 var(--space-md)", color: "var(--text-muted)",
                    fontSize: "0.875rem" }}>{subtitle}</p>
      )}
      {children}
    </Card>
  );
}

function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" style={{ color: "var(--danger)", fontSize: "0.875rem" }}>{message}</p>
  );
}

/* ── SCIM tokens ─────────────────────────────────────────────────── */

function ScimTokensSection() {
  const [tokens, setTokens] = useState<ScimToken[] | null>(null);
  const [name, setName] = useState("");
  const [tenant, setTenant] = useState("");
  const [minted, setMinted] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setTokens(await listScimTokens()); }
    catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const mint = async () => {
    if (!name.trim()) { setError("Give the token a name — it is how you will know what to revoke."); return; }
    setBusy(true); setError(null);
    try {
      const created = await createScimToken({ name: name.trim(), tenant_id: tenant.trim() || null });
      setMinted(created.token);
      setName(""); setTenant("");
      await load();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  const revoke = async (id: string) => {
    setBusy(true);
    try { await revokeScimToken(id); await load(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Section
      title="SCIM tokens"
      subtitle="The credential the customer's identity provider holds. It provisions and deprovisions accounts for its tenant, so treat it like a password — and revoke rather than delete, so the row still explains why the IdP started getting 401s."
    >
      <ErrorNote message={error} />

      {minted && (
        <div style={{
          background: "var(--warning-bg, #3a2f00)", border: "1px solid var(--warning, #c90)",
          borderRadius: "var(--radius)", padding: "var(--space-md)",
          marginBottom: "var(--space-md)",
        }}>
          <strong>Copy this now — it is shown once.</strong>
          <p style={{ fontSize: "0.8125rem", margin: "var(--space-xs) 0" }}>
            Only a SHA-256 of it is stored, so it cannot be shown again. If you lose it,
            revoke this token and mint another.
          </p>
          <code style={{ display: "block", wordBreak: "break-all", padding: "var(--space-sm)",
                         background: "var(--bg-elevated)", borderRadius: "var(--radius-sm)" }}>
            {minted}
          </code>
          <Button variant="secondary" onClick={() => setMinted(null)}
                  style={{ marginTop: "var(--space-sm)" }}>
            I have stored it
          </Button>
        </div>
      )}

      <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap",
                    marginBottom: "var(--space-md)" }}>
        <input value={name} onChange={e => setName(e.target.value)}
               placeholder="Name (e.g. Entra ID production)" aria-label="Token name"
               style={{ flex: "1 1 16rem" }} />
        <input value={tenant} onChange={e => setTenant(e.target.value)}
               placeholder="Tenant slug (blank = all tenants)" aria-label="Tenant"
               style={{ flex: "1 1 12rem" }} />
        <Button onClick={mint} disabled={busy}>Mint token</Button>
      </div>

      {tokens === null ? <Spinner /> : tokens.length === 0 ? (
        <EmptyState title="No SCIM tokens" description="Mint one and paste it into the IdP's provisioning settings." />
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead><tr>
            <th style={cellStyle}>Name</th>
            <th style={cellStyle}>Tenant</th>
            <th style={cellStyle}>Last used</th>
            <th style={cellStyle}>Status</th>
            <th style={cellStyle} />
          </tr></thead>
          <tbody>
            {tokens.map(t => (
              <tr key={t.id}>
                <td style={cellStyle}>{t.name}</td>
                <td style={cellStyle}>{t.tenant_id ?? "all"}</td>
                <td style={cellStyle}>
                  {t.last_used_at ? new Date(t.last_used_at).toLocaleString() : "never"}
                </td>
                <td style={cellStyle}>
                  {t.revoked_at ? "revoked" : "active"}
                </td>
                <td style={cellStyle}>
                  {!t.revoked_at && (
                    <Button variant="danger" onClick={() => revoke(t.id)} disabled={busy}>
                      Revoke
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

/* ── group mappings ──────────────────────────────────────────────── */

function GroupMappingsSection() {
  const [mappings, setMappings] = useState<IdentityGroupMapping[] | null>(null);
  const [groups, setGroups] = useState<{ id: string; name: string; tenant_id: string }[]>([]);
  const [unmapped, setUnmapped] = useState<{ source: string; external_group: string; seen: number }[]>([]);
  const [form, setForm] = useState({ tenant: "", source: "scim", external_group: "", access_group_id: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [m, g, u] = await Promise.all([
        listGroupMappings(), listAccessGroupsBrief(), listUnmappedGroups(),
      ]);
      setMappings(m); setGroups(g); setUnmapped(u);
    } catch (e) { setError(String(e)); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    if (!form.tenant.trim() || !form.external_group.trim() || !form.access_group_id) {
      setError("Tenant, IdP group and access group are all required.");
      return;
    }
    setBusy(true); setError(null);
    try {
      await createGroupMapping({
        tenant: form.tenant.trim(), source: form.source,
        external_group: form.external_group.trim(),
        access_group_id: form.access_group_id,
      });
      setForm({ ...form, external_group: "" });
      await load();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Section
      title="IdP group mappings"
      subtitle="An IdP group grants nothing until it is mapped here. There is deliberately no fallback that matches an access group by name — the IdP's administrator owns those strings, and a name match would let them grant Velaris authorization by naming a directory group after one of ours."
    >
      <ErrorNote message={error} />

      {unmapped.length > 0 && (
        <div style={{ marginBottom: "var(--space-md)", fontSize: "0.875rem" }}>
          <strong>Asserted but not mapped:</strong>{" "}
          {unmapped.map(u => (
            <button key={`${u.source}:${u.external_group}`} type="button"
                    onClick={() => setForm({ ...form, source: u.source, external_group: u.external_group })}
                    title={`Seen ${u.seen}× from ${SOURCE_LABELS[u.source] ?? u.source} — click to map`}
                    style={{ marginRight: "var(--space-xs)", cursor: "pointer" }}>
              {u.external_group}
            </button>
          ))}
          <p style={{ color: "var(--text-muted)", marginTop: "var(--space-xs)" }}>
            These arrived in an assertion or a SCIM sync and matched no mapping, so they
            granted nothing. Click one to fill the form.
          </p>
        </div>
      )}

      <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap",
                    marginBottom: "var(--space-md)" }}>
        <input value={form.tenant} onChange={e => setForm({ ...form, tenant: e.target.value })}
               placeholder="Tenant slug or UUID" aria-label="Tenant" style={{ flex: "1 1 10rem" }} />
        <select value={form.source} onChange={e => setForm({ ...form, source: e.target.value })}
                aria-label="Source">
          {SOURCES.map(s => <option key={s} value={s}>{SOURCE_LABELS[s]}</option>)}
        </select>
        <input value={form.external_group}
               onChange={e => setForm({ ...form, external_group: e.target.value })}
               placeholder="IdP group name or object id" aria-label="IdP group"
               style={{ flex: "1 1 14rem" }} />
        <select value={form.access_group_id} aria-label="Access group"
                onChange={e => setForm({ ...form, access_group_id: e.target.value })}>
          <option value="">Access group…</option>
          {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
        <Button onClick={create} disabled={busy}>Map</Button>
      </div>

      {mappings === null ? <Spinner /> : mappings.length === 0 ? (
        <EmptyState title="No mappings"
                    description="Until a group is mapped, a federated user signs in with no access group at all." />
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead><tr>
            <th style={cellStyle}>IdP group</th>
            <th style={cellStyle}>Source</th>
            <th style={cellStyle}>Access group</th>
            <th style={cellStyle}>Added by</th>
            <th style={cellStyle} />
          </tr></thead>
          <tbody>
            {mappings.map(m => (
              <tr key={m.id}>
                <td style={cellStyle}>{m.external_group}</td>
                <td style={cellStyle}>{SOURCE_LABELS[m.source] ?? m.source}</td>
                <td style={cellStyle}>{m.access_group_name ?? m.access_group_id}</td>
                <td style={cellStyle}>{m.created_by}</td>
                <td style={cellStyle}>
                  <Button variant="danger" disabled={busy}
                          onClick={async () => {
                            setBusy(true);
                            try { await deleteGroupMapping(m.id); await load(); }
                            catch (e) { setError(String(e)); }
                            finally { setBusy(false); }
                          }}>
                    Unmap
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

/* ── account ↔ IdP identity links ────────────────────────────────── */

function AccountLinksSection() {
  const [rows, setRows] = useState<AccountLink[] | null>(null);
  const [query, setQuery] = useState("");
  const [showUnlinked, setShowUnlinked] = useState(false);
  const [form, setForm] = useState({
    identifier: "", external_id: "", sso_provider: "", sso_subject: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await listAccountLinks({
        q: query.trim() || undefined, linked_only: !showUnlinked,
      }));
    } catch (e) { setError(String(e)); }
  }, [query, showUnlinked]);

  useEffect(() => { void load(); }, [load]);

  const link = async () => {
    const external = form.external_id.trim();
    const provider = form.sso_provider.trim();
    const subject = form.sso_subject.trim();
    if (!form.identifier.trim() || (!external && !(provider && subject))) {
      // A half-supplied OIDC pair is the dangerous shape, so it is refused here
      // as well as on the server: `(provider, NULL)` would match every account
      // that has never used that provider.
      setError("Give an account, and either a SAML NameID or both an OIDC provider and subject.");
      return;
    }
    setBusy(true); setError(null);
    try {
      await linkAccount(form.identifier.trim(), {
        ...(external ? { external_id: external } : {}),
        ...(provider && subject ? { sso_provider: provider, sso_subject: subject } : {}),
      });
      setForm({ identifier: "", external_id: "", sso_provider: "", sso_subject: "" });
      await load();
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Section
      title="Linked accounts"
      subtitle="A SAML or OIDC login will not adopt an account it matches only by email address — an assertion naming an address is not proof of owning the account that holds it, and the allowlisted domain is exactly the one those addresses are in. Somebody who already had an account before SSO is linked here instead: paste the identity the IdP sends for them, once — the NameID for SAML, or the provider and subject for OIDC, which key on different fields."
    >
      <ErrorNote message={error} />

      <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap",
                    marginBottom: "var(--space-md)" }}>
        <input value={form.identifier}
               onChange={e => setForm({ ...form, identifier: e.target.value })}
               placeholder="Account — email, username or id" aria-label="Account"
               style={{ flex: "1 1 16rem" }} />
        <input value={form.external_id}
               onChange={e => setForm({ ...form, external_id: e.target.value })}
               placeholder="SAML NameID / externalId" aria-label="External id"
               style={{ flex: "1 1 14rem" }} />
        <input value={form.sso_provider}
               onChange={e => setForm({ ...form, sso_provider: e.target.value })}
               placeholder="OIDC provider (e.g. google)" aria-label="OIDC provider"
               style={{ flex: "1 1 10rem" }} />
        <input value={form.sso_subject}
               onChange={e => setForm({ ...form, sso_subject: e.target.value })}
               placeholder="OIDC subject (sub)" aria-label="OIDC subject"
               style={{ flex: "1 1 12rem" }} />
        <Button onClick={link} disabled={busy}>Link</Button>
      </div>

      <div style={{ display: "flex", gap: "var(--space-sm)", alignItems: "center",
                    flexWrap: "wrap", marginBottom: "var(--space-md)" }}>
        <input value={query} onChange={e => setQuery(e.target.value)}
               placeholder="Search username, email, NameID or OIDC subject" aria-label="Search"
               style={{ flex: "1 1 16rem" }} />
        <label style={{ fontSize: "0.875rem" }}>
          <input type="checkbox" checked={showUnlinked}
                 onChange={e => setShowUnlinked(e.target.checked)} />{" "}
          include accounts with no link
        </label>
      </div>

      {rows === null ? <Spinner /> : rows.length === 0 ? (
        <EmptyState title="No linked accounts"
                    description="Federated users provisioned by the IdP are linked automatically; this list is for the ones that existed here first." />
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead><tr>
            <th style={cellStyle}>Account</th>
            <th style={cellStyle}>IdP identity</th>
            <th style={cellStyle}>State</th>
            <th style={cellStyle} />
          </tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.user_id}>
                <td style={cellStyle}>
                  {r.display_name || r.username}
                  <div style={{ color: "var(--text-muted)" }}>{r.email}</div>
                </td>
                <td style={cellStyle}>
                  {r.external_id && <div>{r.external_id}</div>}
                  {r.sso_subject && (
                    <div style={{ color: "var(--text-muted)" }}>
                      {r.sso_provider}: {r.sso_subject}
                    </div>
                  )}
                  {!r.external_id && !r.sso_subject && (
                    <span style={{ color: "var(--text-muted)" }}>not linked</span>
                  )}
                </td>
                <td style={cellStyle}>
                  {r.is_active ? "active" : "disabled"}
                  {r.has_password && (
                    /* Worth saying out loud: after linking, this account has two
                       ways in — its password and the IdP. */
                    <div style={{ color: "var(--warning, #c90)" }}>has a password</div>
                  )}
                </td>
                <td style={cellStyle}>
                  {(r.external_id || r.sso_subject) && (
                    <Button variant="danger" disabled={busy}
                            onClick={async () => {
                              setBusy(true);
                              try { await unlinkAccount(r.username); await load(); }
                              catch (e) { setError(String(e)); }
                              finally { setBusy(false); }
                            }}>
                      Unlink
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

/* ── SAML metadata + assertion tester ────────────────────────────── */

/** What the ACS would do with this assertion, in its own words.
 *
 *  The tester used to report only "would auto-provision: yes / no — the domain
 *  is not on the allowlist", which names the wrong cause for every refusal that
 *  is not the allowlist — and sends the administrator hunting through an
 *  attribute map for a fault that is not there. */
function describeOutcome(result: any): string {
  switch (result.outcome) {
    case "would_sign_in_as_existing":
      return `signs in as the linked account "${result.linked_username}"`;
    case "would_create":
      return "creates a new account";
    case "would_link_existing":
      return `links the existing account "${result.email_held_by}" `
        + "(link_existing_by_email is on)";
    case "email_taken":
      return `refused — "${result.email_held_by}" already uses this address and is `
        + "not linked to this provider. Link it deliberately, or set "
        + "link_existing_by_email on the provider.";
    case "local_account_protected":
      return `refused — "${result.email_held_by}" signs in here with a password, `
        + "so an assertion may never adopt it.";
    case "domain_not_allowed":
      return "refused — the email domain is not on the allowlist";
    case "auto_provision_off":
      return "refused — auto-provisioning is off for this provider";
    case "no_email":
      return "refused — the assertion carries no email address";
    default:
      return result.would_provision ? "would provision" : "refused";
  }
}

function SamlSection() {
  const [tenant, setTenant] = useState("");
  const [assertion, setAssertion] = useState("");
  const [result, setResult] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const metadataUrl = tenant.trim()
    ? `${window.location.origin}/api/v1/auth/saml/${encodeURIComponent(tenant.trim())}/metadata`
    : null;

  const run = async () => {
    if (!tenant.trim() || !assertion.trim()) {
      setError("Both the tenant and a base64 SAMLResponse are needed.");
      return;
    }
    setBusy(true); setError(null); setResult(null);
    try { setResult(await testSamlAssertion(tenant.trim(), assertion.trim())); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };

  return (
    <Section
      title="SAML"
      subtitle="Configure the provider itself on the SSO tab with provider name 'saml'. This is the part the IdP team needs from you, and the part you need when their assertion does not map the way you expected."
    >
      <ErrorNote message={error} />

      <div style={{ display: "flex", gap: "var(--space-sm)", flexWrap: "wrap",
                    marginBottom: "var(--space-md)" }}>
        <input value={tenant} onChange={e => setTenant(e.target.value)}
               placeholder="Tenant slug" aria-label="Tenant slug"
               style={{ flex: "1 1 12rem" }} />
      </div>

      {metadataUrl && (
        <p style={{ fontSize: "0.875rem" }}>
          <strong>SP metadata URL</strong> — give this to the IdP administrator:{" "}
          <code style={{ wordBreak: "break-all" }}>{metadataUrl}</code>
        </p>
      )}

      <label style={{ display: "block", marginTop: "var(--space-md)", fontSize: "0.875rem" }}>
        Paste a base64 SAMLResponse to see how it maps. Nothing is issued, nobody is
        provisioned, and the assertion is not consumed — so the same capture can be
        pasted again after an attribute-map change.
        <textarea value={assertion} onChange={e => setAssertion(e.target.value)}
                  rows={5} aria-label="SAMLResponse"
                  style={{ width: "100%", marginTop: "var(--space-xs)", fontFamily: "monospace" }} />
      </label>
      <Button onClick={run} disabled={busy} style={{ marginTop: "var(--space-sm)" }}>
        Test assertion
      </Button>

      {result && (
        <div style={{ marginTop: "var(--space-md)", fontSize: "0.875rem" }}>
          {result.ok === false ? (
            <p style={{ color: "var(--danger)" }}>
              Refused: <code>{result.reason}</code>{result.detail ? ` — ${result.detail}` : ""}
            </p>
          ) : (
            <>
              <p>
                <strong>{result.display_name || result.name_id}</strong>{" "}
                &lt;{result.email ?? "no email claim"}&gt;
              </p>
              <p>
                Mapped groups:{" "}
                {(result.mapped_groups ?? []).length
                  ? result.mapped_groups.map((g: any) => g.group).join(", ")
                  : "none"}
              </p>
              {(result.unmapped_groups ?? []).length > 0 && (
                <p style={{ color: "var(--warning, #c90)" }}>
                  Unmapped (granting nothing): {result.unmapped_groups.join(", ")}
                </p>
              )}
              <p>
                Sign-in outcome: {describeOutcome(result)}
              </p>
            </>
          )}
        </div>
      )}
    </Section>
  );
}

/* ── provisioning log ────────────────────────────────────────────── */

function ProvisionLogSection() {
  const [entries, setEntries] = useState<ProvisionLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProvisionLog({ limit: 100 }).then(setEntries).catch(e => setError(String(e)));
  }, []);

  return (
    <Section
      title="Provisioning log"
      subtitle="Every decision from every source, refusals included. This is where 'why is this person in this group?' is answered."
    >
      <ErrorNote message={error} />
      {entries === null ? <Spinner /> : entries.length === 0 ? (
        <EmptyState title="Nothing provisioned yet"
                    description="Entries appear once an IdP starts syncing or somebody signs in with SAML." />
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8125rem" }}>
          <thead><tr>
            <th style={cellStyle}>When</th>
            <th style={cellStyle}>Source</th>
            <th style={cellStyle}>User</th>
            <th style={cellStyle}>Action</th>
            <th style={cellStyle}>Detail</th>
          </tr></thead>
          <tbody>
            {entries.map(e => (
              <tr key={e.id}>
                <td style={cellStyle}>
                  {e.created_at ? new Date(e.created_at).toLocaleString() : ""}
                </td>
                <td style={cellStyle}>{SOURCE_LABELS[e.source] ?? e.source}</td>
                <td style={cellStyle}>{e.username ?? e.external_id ?? "—"}</td>
                <td style={cellStyle}>
                  {e.action}
                  {e.result === "error" && (
                    <span style={{ color: "var(--danger)" }}> ({e.detail?.reason})</span>
                  )}
                </td>
                <td style={cellStyle}>
                  {(e.detail?.groups_added ?? []).length > 0 && `+${e.detail.groups_added.length} group(s) `}
                  {(e.detail?.groups_removed ?? []).length > 0 && `-${e.detail.groups_removed.length} group(s) `}
                  {(e.detail?.unmapped_groups ?? []).length > 0 && (
                    <span style={{ color: "var(--warning, #c90)" }}>
                      {e.detail.unmapped_groups.length} unmapped
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

export default function IdentityTab() {
  return (
    <div>
      <SamlSection />
      <AccountLinksSection />
      <ScimTokensSection />
      <GroupMappingsSection />
      <ProvisionLogSection />
    </div>
  );
}
