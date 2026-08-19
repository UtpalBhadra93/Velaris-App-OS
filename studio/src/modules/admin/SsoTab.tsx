/* ═══════════════════════════════════════════════════════════════════
   Admin Console — SSO providers
   ═══════════════════════════════════════════════════════════════════

   Registering an SSO provider used to be an API-only operation: there was no
   screen anywhere in Studio, so the only way to configure Google or GitHub
   sign-in was a hand-rolled POST with a bearer token.

   The two fields that decide who gets an account — the domain allowlist and
   `default_roles` — are given real inputs rather than a raw JSON box on
   purpose. An empty allowlist means "no auto-provisioning", so a mistyped key
   in a free-form editor would silently switch provisioning off (or, typed the
   other way, silently on). Anything else in `config` still gets a key/value
   grid, since the column is open-ended.

   **SAML rows are a second shape in the same table.** `provider='saml'` has no
   client secret, no OAuth endpoints and a different allowlist key
   (`email_domain_allowlist`, not `allowed_domains`). Before this screen knew
   that, it read the OIDC key for every row: a SAML provider with a live
   allowlist was summarised as *"Auto-provisioning: off — existing accounts
   only"*, and editing it through the form would have written the OIDC key —
   which the SAML path never reads — while dropping the real one. Every SAML
   field the login path actually consults now has an input here, and the two
   that decide whether the button appears at all (the ACS URL's tenant segment,
   the signing certificate) are validated before the save.

   Copyright (c) 2024-2026 Velaris Contributors
   SPDX-License-Identifier: BSL-1.1
   ═══════════════════════════════════════════════════════════════════ */
import React, { useCallback, useState } from "react";

import { useApi } from "@shared/hooks";
import {
  createSsoProvider,
  deleteSsoProvider,
  generateSamlSpKey,
  listSsoProvidersAdmin,
  parseSamlMetadata,
  updateSsoProvider,
  type SsoProviderAdmin,
} from "@shared/api/client";
import { Button, Card, EmptyState, Spinner } from "@shared/components";

/** Providers the backend knows end to end. Anything else is accepted as free
 *  text, but then the admin must supply its OAuth endpoints — the server has
 *  no other way to learn them, and the create endpoint refuses a provider it
 *  could never complete a sign-in with. */
const PRESETS: Record<string, string> = {
  google: "Google",
  github: "GitHub",
};

/** `saml` is not a preset — it is a different protocol living in the same
 *  table, so it gets its own branch rather than a row of OAuth endpoints. */
const SAML = "saml";

const providerLabel = (p: string) => PRESETS[p] ?? (p === SAML ? "SAML 2.0" : p);

/** Endpoint keys a custom provider must carry, mirroring the backend's
 *  _CUSTOM_ENDPOINT_KEYS. `emails` is optional and GitHub-shaped. */
const ENDPOINT_FIELDS: { key: string; label: string; placeholder: string; required: boolean; help?: string }[] = [
  { key: "auth_url",  label: "Authorization URL", placeholder: "https://example.okta.com/oauth2/v1/authorize", required: true },
  { key: "token_url", label: "Token URL",         placeholder: "https://example.okta.com/oauth2/v1/token",     required: true,
    help: "The client secret is POSTed here, so it must be https and must not resolve to internal infrastructure." },
  { key: "userinfo",  label: "Userinfo URL",      placeholder: "https://example.okta.com/oauth2/v1/userinfo",  required: true },
  { key: "scope",     label: "Scope",             placeholder: "openid email profile",                          required: true },
];

const ROLES = [
  "viewer", "case_worker", "designer", "manager",
  "integration", "devops", "security", "admin",
];

/** Free mailbox providers. Allowlisting one grants an account to anyone who
 *  can sign up there, which is almost never what an admin means. */
const PUBLIC_DOMAINS = [
  "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
  "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
  "aol.com", "gmx.com", "mail.com", "yandex.com", "zoho.com",
];

const ENDPOINT_KEYS = ["auth_url", "token_url", "userinfo", "scope", "emails"];

/** Rendered by the login page for every provider, SAML or OAuth. */
const BRANDING_KEYS = ["display_name", "icon_url"];

/** Everything `identity/saml.py:SamlConfig` reads. Keys not in this list fall
 *  through to the free-form grid; keys in it must have a real input, or the
 *  grid would be the only way to reach a field the login path depends on. */
const SAML_CONFIG_KEYS = [
  "idp_entity_id", "idp_sso_url", "idp_slo_url", "idp_x509_certs",
  "sp_entity_id", "acs_url", "name_id_format", "clock_skew_seconds",
  "attribute_map", "allowed_relay_state_prefixes", "auto_provision",
  "email_domain_allowlist", "default_roles", "link_existing_by_email",
  // Written by the form, and read back by the scheduled refresh. `metadata_sync`
  // is the refresh's own state — reserved so the advanced grid cannot forge it.
  "idp_metadata_url", "metadata_sync",
  "sign_authn_requests", "slo_enabled",
];

/** Which `config` keys the typed fields own, so the advanced grid never offers
 *  a second, conflicting way to write one. */
const reservedKeys = (provider: string) =>
  provider === SAML
    ? [...SAML_CONFIG_KEYS, ...BRANDING_KEYS]
    : ["allowed_domains", "default_roles", ...ENDPOINT_KEYS, ...BRANDING_KEYS];

/** The allowlist key differs by protocol, and writing the wrong one is silent:
 *  the login path simply never finds an allowlist and refuses everybody. */
const domainKey = (provider: string) =>
  provider === SAML ? "email_domain_allowlist" : "allowed_domains";

const NAME_ID_FORMATS: { value: string; label: string }[] = [
  { value: "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",   label: "Persistent (recommended)" },
  { value: "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress", label: "Email address" },
  { value: "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",  label: "Unspecified" },
];

/** The logical fields `SamlConfig.attribute_map` can override, with the names
 *  the backend already tries when the admin has said nothing. */
const ATTRIBUTE_FIELDS: { key: string; label: string; tried: string }[] = [
  { key: "email",        label: "Email",        tried: "email, mail, …/claims/emailaddress" },
  { key: "display_name", label: "Display name", tried: "displayName, name, cn" },
  { key: "given_name",   label: "First name",   tried: "firstName, givenName" },
  { key: "family_name",  label: "Last name",    tried: "lastName, surname, sn" },
  { key: "groups",       label: "Groups",       tried: "groups, memberOf" },
  { key: "manager",      label: "Manager",      tried: "manager, managerUserId" },
];

const DEFAULT_CLOCK_SKEW = "120";

/** …/auth/saml/{tenant}/acs — the shape `_acs_matches_route` requires. The
 *  tenant segment is not decoration: `Destination` and `Recipient` are compared
 *  against this fixed string, so the route is what ties an assertion to one
 *  tenant. A URL of any other shape also means the login page shows no button,
 *  because `_login_url` derives the sign-in path from exactly this. */
const ACS_PATH = /\/auth\/saml\/([^/]+)\/acs\/?$/;

function acsTenant(url: string): string | null {
  try {
    // A relative value is legal input here; the base is only for parsing.
    const path = new URL(url, window.location.origin).pathname;
    return ACS_PATH.exec(path)?.[1] ?? null;
  } catch {
    return null;
  }
}

interface SamlState {
  metadata_url: string;
  sign_authn_requests: boolean;
  slo_enabled: boolean;
  idp_entity_id: string;
  idp_sso_url: string;
  idp_slo_url: string;
  certs: string[];
  sp_entity_id: string;
  acs_url: string;
  name_id_format: string;
  clock_skew: string;
  auto_provision: boolean;
  link_existing_by_email: boolean;
  relay_prefixes: string[];
  attributes: Record<string, string>;
}

/** Config keys the server writes and the form must not own. They are reserved
 *  (so the advanced grid cannot forge them) but still carried through a save,
 *  or every edit would erase the refresh's last result — including the error
 *  the card is supposed to be showing. */
const SERVER_OWNED_KEYS = ["metadata_sync"];

interface FormState {
  provider: string;
  endpoints: Record<string, string>;
  client_id: string;
  client_secret: string;
  tenant_id: string;
  enabled: boolean;
  domains: string[];
  roles: string[];
  display_name: string;
  icon_url: string;
  saml: SamlState;
  preserved: Record<string, any>;
  extra: { key: string; value: string }[];
}

const EMPTY_SAML: SamlState = {
  metadata_url: "",
  sign_authn_requests: false,
  slo_enabled: false,
  idp_entity_id: "", idp_sso_url: "", idp_slo_url: "", certs: [""],
  sp_entity_id: "", acs_url: "", name_id_format: NAME_ID_FORMATS[0].value,
  clock_skew: DEFAULT_CLOCK_SKEW, auto_provision: true,
  link_existing_by_email: false,
  // The backend's own default is ["/studio"], which is not a route in this SPA
  // — a login through it completed and landed on a blank page. A new provider
  // written from this screen starts at the app root instead.
  relay_prefixes: ["/"],
  attributes: {},
};

const EMPTY_FORM: FormState = {
  provider: "google", endpoints: {}, client_id: "", client_secret: "", tenant_id: "",
  enabled: true, domains: [""], roles: ["viewer"], display_name: "", icon_url: "",
  saml: EMPTY_SAML, preserved: {}, extra: [],
};

const asList = (v: unknown): string[] =>
  Array.isArray(v) ? v.map(x => String(x)) : [];

function formFromRow(p: SsoProviderAdmin): FormState {
  const cfg = p.config ?? {};
  const domains = asList(cfg[domainKey(p.provider)]);
  const roles = asList(cfg.default_roles);
  const certs = asList(cfg.idp_x509_certs);
  const prefixes = asList(cfg.allowed_relay_state_prefixes);
  const attrMap = (cfg.attribute_map ?? {}) as Record<string, unknown>;
  return {
    provider: p.provider,
    endpoints: Object.fromEntries(
      ENDPOINT_KEYS.map(k => [k, typeof cfg[k] === "string" ? (cfg[k] as string) : ""]),
    ),
    client_id: p.client_id,
    client_secret: "",
    tenant_id: p.tenant_id ?? "",
    enabled: p.enabled,
    domains: domains.length ? [...domains] : [""],
    roles: roles.length ? [...roles] : ["viewer"],
    display_name: typeof cfg.display_name === "string" ? cfg.display_name : "",
    icon_url: typeof cfg.icon_url === "string" ? cfg.icon_url : "",
    saml: {
      metadata_url: String(cfg.idp_metadata_url ?? ""),
      sign_authn_requests: cfg.sign_authn_requests === true,
      slo_enabled: cfg.slo_enabled === true,
      idp_entity_id: String(cfg.idp_entity_id ?? ""),
      idp_sso_url: String(cfg.idp_sso_url ?? ""),
      idp_slo_url: String(cfg.idp_slo_url ?? ""),
      certs: certs.length ? certs : [""],
      sp_entity_id: String(cfg.sp_entity_id ?? ""),
      acs_url: String(cfg.acs_url ?? ""),
      name_id_format: String(cfg.name_id_format ?? NAME_ID_FORMATS[0].value),
      clock_skew: String(cfg.clock_skew_seconds ?? DEFAULT_CLOCK_SKEW),
      // Absent means the backend default, which is ON. Reading it as `false`
      // here would turn provisioning off on the next save of an untouched row.
      auto_provision: cfg.auto_provision !== false,
      link_existing_by_email: cfg.link_existing_by_email === true,
      relay_prefixes: prefixes.length ? prefixes : ["/"],
      attributes: Object.fromEntries(
        ATTRIBUTE_FIELDS.map(a => {
          const v = attrMap[a.key];
          return [a.key, Array.isArray(v) ? v.map(String).join(", ") : v ? String(v) : ""];
        }),
      ),
    },
    preserved: Object.fromEntries(
      SERVER_OWNED_KEYS.filter(k => cfg[k] !== undefined).map(k => [k, cfg[k]]),
    ),
    extra: Object.entries(cfg)
      .filter(([k]) => !reservedKeys(p.provider).includes(k))
      .map(([key, value]) => ({
        key,
        value: typeof value === "string" ? value : JSON.stringify(value),
      })),
  };
}

const cleanDomainList = (xs: string[]) =>
  xs.map(d => d.trim().toLowerCase().replace(/^@/, "")).filter(Boolean);

function configFromForm(f: FormState): Record<string, any> {
  const cfg: Record<string, any> = {
    [domainKey(f.provider)]: cleanDomainList(f.domains),
    default_roles: f.roles,
  };

  if (f.provider === SAML) {
    const s = f.saml;
    // Kept so the scheduled refresh knows where to look. Absent = no refresh,
    // which is a legitimate choice for an air-gapped or manually-managed IdP.
    if (s.metadata_url.trim()) cfg.idp_metadata_url = s.metadata_url.trim();
    cfg.sign_authn_requests = s.sign_authn_requests;
    cfg.slo_enabled = s.slo_enabled;
    cfg.idp_entity_id = s.idp_entity_id.trim();
    cfg.idp_sso_url = s.idp_sso_url.trim();
    if (s.idp_slo_url.trim()) cfg.idp_slo_url = s.idp_slo_url.trim();
    cfg.idp_x509_certs = s.certs.map(c => c.trim()).filter(Boolean);
    cfg.sp_entity_id = s.sp_entity_id.trim();
    cfg.acs_url = s.acs_url.trim();
    cfg.name_id_format = s.name_id_format;
    cfg.clock_skew_seconds = Number(s.clock_skew) || Number(DEFAULT_CLOCK_SKEW);
    cfg.auto_provision = s.auto_provision;
    cfg.link_existing_by_email = s.link_existing_by_email;
    cfg.allowed_relay_state_prefixes = s.relay_prefixes.map(p => p.trim()).filter(Boolean);
    const attrs = Object.fromEntries(
      ATTRIBUTE_FIELDS
        .map(a => [a.key, (s.attributes[a.key] ?? "").trim()] as const)
        .filter(([, v]) => v)
        // A comma-separated value means "try these in order", which is the list
        // form `_mapped` already accepts.
        .map(([k, v]) => [k, v.includes(",") ? v.split(",").map(x => x.trim()).filter(Boolean) : v]),
    );
    if (Object.keys(attrs).length) cfg.attribute_map = attrs;
  } else {
    // A preset's endpoints are ignored by the backend (google always means
    // Google), so writing them would only create config that lies.
    if (!(f.provider in PRESETS)) {
      for (const k of ENDPOINT_KEYS) {
        const v = (f.endpoints[k] ?? "").trim();
        if (v) cfg[k] = v;
      }
    }
  }

  Object.assign(cfg, f.preserved);

  if (f.display_name.trim()) cfg.display_name = f.display_name.trim();
  if (f.icon_url.trim()) cfg.icon_url = f.icon_url.trim();

  const reserved = reservedKeys(f.provider);
  for (const { key, value } of f.extra) {
    const k = key.trim();
    // The grid says these are managed above and cannot be set here. It is
    // applied last, so without this it would quietly win — an `acs_url` typed
    // in the grid would overwrite the validated one, which is exactly the
    // "free-form editor silently changes a security decision" the typed fields
    // exist to prevent.
    if (!k || reserved.includes(k)) continue;
    // Accept JSON so a list or number survives the round trip; fall back to the
    // raw string, which is what most values will be.
    try { cfg[k] = JSON.parse(value); } catch { cfg[k] = value; }
  }
  return cfg;
}

/** A same-origin path, and nothing a browser will read as another host.
 *
 *  `//host` is the obvious scheme-relative form, but a browser normalises `\`
 *  to `/` inside an http(s) URL and strips tab and newline before parsing, so
 *  `/\evil.com` and `/<tab>/evil.com` load off-origin while passing a
 *  leading-slash test. Mirrors `identity/saml.py:is_safe_local_path`. */
const isSafeLocalPath = (v: string) =>
  v.startsWith("/") && !v.startsWith("//") && !v.includes("\\")
  // eslint-disable-next-line no-control-regex
  && !/[\s\u0000-\u001f\u007f]/.test(v);

/** Same rule the backend applies before it will publish an icon: a remote URL
 *  on the sign-in page would tell that host who is about to log in. */
const iconAllowed = (v: string) => {
  const t = v.trim();
  return !t || t.startsWith("data:image/") || isSafeLocalPath(t);
};

const MAX_CLOCK_SKEW = 600;

/** Matches `saml_metadata.CERT_WARN_DAYS`. */
const CERT_WARN_DAYS = 30;

// ── shared field styling ──────────────────────────────────────────
const label: React.CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, letterSpacing: 0.3,
  textTransform: "uppercase", color: "var(--text-muted)", marginBottom: 6,
};
const input: React.CSSProperties = {
  width: "100%", padding: "8px 10px", fontSize: 13, borderRadius: 6,
  border: "1px solid var(--border)", background: "var(--bg-root)",
  color: "var(--text-primary)", fontFamily: "inherit",
};
const row: React.CSSProperties = { marginBottom: "var(--space-md)" };
const hint: React.CSSProperties = { fontSize: 11, color: "var(--text-muted)", marginTop: 5, lineHeight: 1.6 };
const panel: React.CSSProperties = {
  ...row, padding: "12px 14px", borderRadius: 6,
  background: "var(--bg-elevated)", border: "1px solid var(--border)",
};
const warn: React.CSSProperties = {
  marginTop: 8, padding: "8px 10px", borderRadius: 6, fontSize: 12, lineHeight: 1.6,
  background: "rgba(220,38,38,0.10)", border: "1px solid rgba(220,38,38,0.45)",
  color: "var(--text-primary)",
};

function Field({ title, children, help }: { title: string; children: React.ReactNode; help?: React.ReactNode }) {
  return (
    <div style={row}>
      <label style={label}>{title}</label>
      {children}
      {help && <div style={hint}>{help}</div>}
    </div>
  );
}

/** A repeating single-line list (domains, certs, relay prefixes). */
function ListField({ title, values, onChange, placeholder, addLabel, multiline, help }: {
  title: string;
  values: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  addLabel: string;
  multiline?: boolean;
  help?: React.ReactNode;
}) {
  const set = (i: number, v: string) => onChange(values.map((x, j) => (j === i ? v : x)));
  const drop = (i: number) => {
    const next = values.filter((_, j) => j !== i);
    onChange(next.length ? next : [""]);
  };
  return (
    <div style={row}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <label style={{ ...label, marginBottom: 0 }}>{title}</label>
        <Button size="sm" variant="secondary" onClick={() => onChange([...values, ""])}>{addLabel}</Button>
      </div>
      {values.map((v, i) => (
        <div key={i} style={{ display: "flex", gap: 8, marginBottom: 6 }}>
          {multiline ? (
            <textarea
              style={{ ...input, flex: 1, minHeight: 76, fontFamily: "monospace", fontSize: 11 }}
              value={v} spellCheck={false} placeholder={placeholder}
              onChange={e => set(i, e.target.value)}
            />
          ) : (
            <input
              style={{ ...input, flex: 1 }} value={v} spellCheck={false} placeholder={placeholder}
              onChange={e => set(i, e.target.value)}
            />
          )}
          <Button size="sm" variant="ghost" onClick={() => drop(i)}>✕</Button>
        </div>
      ))}
      {help && <div style={hint}>{help}</div>}
    </div>
  );
}

// ── the form ──────────────────────────────────────────────────────

function ProviderForm({ editing, onDone, onCancel }: {
  editing: SsoProviderAdmin | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [f, setF] = useState<FormState>(editing ? formFromRow(editing) : EMPTY_FORM);
  const [showSecret, setShowSecret] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmPublic, setConfirmPublic] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [metadataXml, setMetadataXml] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyMsg, setKeyMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [spKey, setSpKey] = useState(editing?.saml_health?.sp_key ?? null);

  /** Generate or replace the SP key pair.
   *
   *  Keyed on the provider's tenant, because that is what the endpoint routes
   *  on — the same segment the ACS URL carries. */
  const runGenerateKey = async (replacing: boolean) => {
    const tenantSegment = acsTenant(f.saml.acs_url) ?? f.tenant_id.trim();
    if (!tenantSegment) {
      return setKeyMsg({ text: "Set the tenant and ACS URL first.", ok: false });
    }
    if (replacing && !window.confirm(
      "Replace the key pair? Anything the IdP is already encrypting to the old key "
      + "stops working until you re-import this SP's metadata there.")) return;

    setKeyBusy(true); setKeyMsg(null);
    try {
      const out = await generateSamlSpKey(tenantSegment, replacing);
      setSpKey(out.summary);
      setKeyMsg({ text: out.next_step, ok: true });
    } catch (e: any) {
      setKeyMsg({ text: e?.message || "Could not generate a key.", ok: false });
    } finally {
      setKeyBusy(false);
    }
  };

  /** Read the IdP's metadata and fill the fields it covers.
   *
   *  Deliberately fills rather than saves: an import that wrote straight to the
   *  provider would let one fetch repoint a live sign-in. The admin sees what
   *  changed and presses save. Fields the metadata does not carry — audience,
   *  ACS, allowlist — are left alone. */
  const runImport = async (src: { url?: string; xml?: string }) => {
    setImportMsg(null);
    if (!src.url?.trim() && !src.xml?.trim()) {
      return setImportMsg({ text: "Give a metadata URL, or paste the XML.", ok: false });
    }
    setImporting(true);
    try {
      const meta = await parseSamlMetadata(src);
      setF(p => ({
        ...p,
        saml: {
          ...p.saml,
          idp_entity_id: meta.idp_entity_id,
          idp_sso_url: meta.idp_sso_url,
          idp_slo_url: meta.idp_slo_url ?? p.saml.idp_slo_url,
          certs: meta.idp_x509_certs.length ? meta.idp_x509_certs : p.saml.certs,
          name_id_format: meta.name_id_format || p.saml.name_id_format,
        },
      }));
      const soonest = meta.certs
        .map(c => c.days_remaining).filter((d): d is number => d !== null)
        .sort((a, b) => a - b)[0];
      setImportMsg({
        ok: true,
        text: `Imported ${meta.idp_x509_certs.length} certificate`
          + `${meta.idp_x509_certs.length === 1 ? "" : "s"} from ${meta.idp_entity_id}`
          + (soonest !== undefined ? ` — expires in ${soonest} days.` : ".")
          + " Review the fields, then save.",
      });
    } catch (e: any) {
      setImportMsg({ text: e?.message || "Could not read that metadata.", ok: false });
    } finally {
      setImporting(false);
    }
  };

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF(p => ({ ...p, [k]: v }));
  const setSaml = <K extends keyof SamlState>(k: K, v: SamlState[K]) =>
    setF(p => ({ ...p, saml: { ...p.saml, [k]: v } }));

  const isSaml = f.provider === SAML;
  const isPreset = f.provider in PRESETS;
  const needsEndpoints = !isPreset && !isSaml && f.provider.trim() !== "";
  const endpointValue = (st: FormState, k: string) => st.endpoints[k] ?? "";
  const setEndpoint = (k: string, v: string) =>
    setF(p => ({ ...p, endpoints: { ...p.endpoints, [k]: v } }));

  const cleanDomains = cleanDomainList(f.domains);
  const publicHits = cleanDomains.filter(d => PUBLIC_DOMAINS.includes(d));
  const origin = window.location.origin;

  // The tenant segment the URLs will carry: whatever the ACS already names,
  // else the tenant field, else a placeholder for the admin to replace.
  const urlTenant = acsTenant(f.saml.acs_url) ?? (f.tenant_id.trim() || "<tenant>");
  const suggestedAcs = `${origin}/api/v1/auth/saml/${encodeURIComponent(f.tenant_id.trim() || "default")}/acs`;

  const save = async () => {
    setError(null);
    if (!f.provider.trim()) return setError("Provider is required.");
    if (!f.roles.length) return setError("Pick at least one default role.");
    if (!iconAllowed(f.icon_url)) {
      return setError("The icon must be a path on this server (starting with /) or a data:image/ URI — a remote URL is dropped.");
    }

    if (isSaml) {
      const s = f.saml;
      // Mirrors SamlConfig.missing(): a row without these is stored happily and
      // then answers 404 at sign-in, which reads as "SAML is not configured".
      const gaps = [
        !s.idp_entity_id.trim() && "IdP Entity ID",
        !s.idp_sso_url.trim() && "IdP SSO URL",
        !s.sp_entity_id.trim() && "SP Entity ID",
        !s.acs_url.trim() && "ACS URL",
        !s.certs.some(c => c.trim()) && "at least one signing certificate",
      ].filter(Boolean) as string[];
      if (gaps.length) return setError(`A SAML provider needs: ${gaps.join(", ")}.`);

      // The sign-in redirect goes to the IdP over this URL. Plain http makes
      // the whole authentication downgradeable by anyone on the path. (The ACS
      // is deliberately not held to this — it is our own URL, and http on
      // localhost is how a dev box runs.)
      const insecure = [
        ["IdP SSO URL", s.idp_sso_url],
        ["IdP single logout URL", s.idp_slo_url],
      ].filter(([, v]) => v.trim() && !v.trim().toLowerCase().startsWith("https://"));
      if (insecure.length) {
        return setError(`${insecure.map(([n]) => n).join(" and ")} must use https://.`);
      }

      const seg = acsTenant(s.acs_url);
      if (!seg) {
        return setError(
          "The ACS URL must end in /auth/saml/<tenant>/acs — the tenant segment is what ties an " +
          "assertion to this tenant, and the sign-in button is derived from it.",
        );
      }
      if (f.tenant_id.trim() && seg !== f.tenant_id.trim()) {
        return setError(
          `The ACS URL names tenant "${seg}" but this provider is scoped to "${f.tenant_id.trim()}". ` +
          "An assertion posted to either one is refused while they disagree.",
        );
      }
      // Not tidiness: the first landing path is used as the redirect target
      // when the IdP sends no RelayState, and that redirect carries a live
      // sign-in code. "//evil.com" and "/\evil.com" are both off-origin to a
      // browser, so a leading slash is not the test.
      const badPrefix = s.relay_prefixes.map(p => p.trim()).filter(Boolean).find(p => !isSafeLocalPath(p));
      if (badPrefix) {
        return setError(
          `"${badPrefix}" is not a path on this server. A landing path starts with a single "/" ` +
          "and contains no backslash or whitespace — anything else can send a completed sign-in, " +
          "and the code it carries, to another host.",
        );
      }
      if (!s.relay_prefixes.some(p => p.trim())) return setError("Give at least one allowed landing path.");
      const skew = Number(s.clock_skew);
      if (!Number.isInteger(skew) || skew < 0 || skew > MAX_CLOCK_SKEW) {
        return setError(
          `Clock skew must be a whole number of seconds between 0 and ${MAX_CLOCK_SKEW}. ` +
          "A larger tolerance stops the assertion's own expiry meaning anything.",
        );
      }
    } else {
      if (!f.client_id.trim()) return setError("Client ID is required.");
      if (!editing && !f.client_secret.trim()) return setError("Client Secret is required.");
      if (needsEndpoints) {
        const missing = ENDPOINT_FIELDS.filter(ef => ef.required && !endpointValue(f, ef.key).trim());
        if (missing.length) {
          return setError(
            `"${f.provider}" is not a built-in provider, so it needs its own endpoints. ` +
            `Missing: ${missing.map(m => m.label).join(", ")}.`,
          );
        }
        const insecure = ENDPOINT_FIELDS
          .filter(ef => ef.key !== "scope")
          .map(ef => endpointValue(f, ef.key).trim())
          .concat(endpointValue(f, "emails").trim())
          .filter(u => u && !u.toLowerCase().startsWith("https://"));
        if (insecure.length) return setError("Endpoint URLs must use https://.");
      }
    }

    // Ask once, then let the admin through — a deliberate public-signup
    // deployment is legitimate, it just must not happen by accident.
    if (publicHits.length && !confirmPublic) return setConfirmPublic(true);

    setSaving(true);
    try {
      const config = configFromForm(f);
      if (editing) {
        await updateSsoProvider(editing.id, {
          // SAML has no client id of its own; leaving the stored value alone
          // keeps an edit from rewriting a field the protocol never reads.
          client_id: isSaml ? editing.client_id : f.client_id.trim(),
          ...(f.client_secret.trim() ? { client_secret: f.client_secret.trim() } : {}),
          enabled: f.enabled,
          tenant_id: f.tenant_id.trim() || null,
          config,
        });
      } else {
        await createSsoProvider({
          provider: f.provider,
          // The create endpoint requires both, and SAML has neither. The SP
          // entity ID is the nearest true thing to record.
          client_id: isSaml ? (f.saml.sp_entity_id.trim() || SAML) : f.client_id.trim(),
          client_secret: isSaml ? "" : f.client_secret.trim(),
          tenant_id: f.tenant_id.trim() || null,
          config,
        });
      }
      onDone();
    } catch (e: any) {
      setError(e?.message || "Could not save the provider.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card style={{ marginBottom: "var(--space-lg)", border: "1px solid var(--accent)" }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: "var(--space-md)" }}>
        {editing ? `Edit ${providerLabel(editing.provider)} provider` : "New SSO provider"}
      </div>

      <Field
        title="Provider"
        help={editing
          ? "The provider cannot be changed — it would re-point this configuration at a different identity provider. Delete and re-create instead."
          : isSaml
            ? "SAML 2.0. No client secret and no OAuth endpoints: the IdP's certificate is what authenticates the assertion."
            : isPreset
              ? `${providerLabel(f.provider)} is built in — its OAuth endpoints are known.`
              : "Not a built-in provider, so enter its OAuth endpoints below. Any OIDC-shaped provider works (Okta, Entra ID, Keycloak, Auth0…)."}
      >
        <input
          style={{ ...input, opacity: editing ? 0.6 : 1 }}
          value={f.provider}
          list="sso-preset-providers"
          spellCheck={false}
          disabled={!!editing}
          placeholder="google, github, saml, okta…"
          onChange={e => set("provider", e.target.value.trim().toLowerCase())}
        />
        <datalist id="sso-preset-providers">
          {[...Object.keys(PRESETS), SAML].map(k => <option key={k} value={k} />)}
        </datalist>
      </Field>

      {/* ── OAuth credentials, which SAML does not have ── */}
      {!isSaml && (
        <>
          <Field title="Client ID" help="Public — it appears in the authorization URL the browser is sent to.">
            <input
              style={input} value={f.client_id} spellCheck={false}
              placeholder={f.provider === "google" ? "…apps.googleusercontent.com" : f.provider === "github" ? "Iv1.…" : "client id from your identity provider"}
              onChange={e => set("client_id", e.target.value)}
            />
          </Field>

          <Field
            title="Client Secret"
            help={editing
              ? "Leave blank to keep the stored secret. Velaris never displays it back, not even to an admin."
              : "Encrypted at rest with a key derived from auth_secret — rotating that secret requires re-entering this one."}
          >
            <div style={{ display: "flex", gap: 8 }}>
              <input
                style={{ ...input, flex: 1 }} value={f.client_secret} spellCheck={false}
                type={showSecret ? "text" : "password"}
                placeholder={editing ? (editing.has_secret ? "•••••••• (unchanged)" : "not set") : ""}
                onChange={e => set("client_secret", e.target.value)}
              />
              <Button size="sm" variant="secondary" onClick={() => setShowSecret(s => !s)}>
                {showSecret ? "Hide" : "Show"}
              </Button>
            </div>
          </Field>
        </>
      )}

      {/* ── endpoints, only for a provider the backend does not know ── */}
      {needsEndpoints && (
        <div style={panel}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>
            OAuth endpoints for “{f.provider}”
          </div>
          <div style={{ ...hint, marginTop: 0, marginBottom: 10 }}>
            Copy these from the provider's OIDC discovery document
            (<code>/.well-known/openid-configuration</code>). Velaris requires an{" "}
            <code>email_verified</code> claim from the userinfo endpoint, exactly as it does for Google —
            an address the provider does not vouch for cannot name a Velaris account.
          </div>
          {ENDPOINT_FIELDS.map(ef => (
            <div key={ef.key} style={{ marginBottom: 10 }}>
              <label style={label}>{ef.label}</label>
              <input
                style={input} spellCheck={false} placeholder={ef.placeholder}
                value={endpointValue(f, ef.key)}
                onChange={e => setEndpoint(ef.key, e.target.value)}
              />
              {ef.help && <div style={hint}>{ef.help}</div>}
            </div>
          ))}
          <div style={{ marginBottom: 4 }}>
            <label style={label}>Verified-emails URL (optional)</label>
            <input
              style={input} spellCheck={false}
              placeholder="only if the provider exposes a GitHub-style /emails list"
              value={endpointValue(f, "emails")}
              onChange={e => setEndpoint("emails", e.target.value)}
            />
            <div style={hint}>
              Leave blank for a normal OIDC provider. Set it only when the userinfo email is
              unverified free text and the verified set lives at a separate URL, as on GitHub.
            </div>
          </div>
        </div>
      )}

      <Field
        title="Tenant"
        help={isSaml
          ? "The tenant this IdP signs users into. It must match the tenant segment of the ACS URL below. Blank means the row serves whichever tenant the ACS URL names."
          : "Blank = available to every tenant. Otherwise a tenant slug or id, and only that tenant sees the button."}
      >
        <input style={input} value={f.tenant_id} spellCheck={false}
               placeholder="(blank = all tenants)"
               onChange={e => set("tenant_id", e.target.value)} />
      </Field>

      {/* ── SAML: the IdP half ── */}
      {isSaml && (
        <div style={panel}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>
            Identity provider
          </div>
          <div style={{ ...hint, marginTop: 0, marginBottom: 10 }}>
            Import them from the IdP's metadata below, or copy them by hand. Okta shows
            them under “View SAML setup instructions”; Entra ID under “Set up single
            sign-on”. Every product names them differently — Okta's <em>Identity Provider
            Issuer</em> and Entra's <em>Microsoft Entra Identifier</em> are both the
            Entity ID here — which is why importing is the shorter road.
          </div>

          {/* ── metadata import ── */}
          <div style={{
            marginBottom: 14, padding: "10px 12px", borderRadius: 6,
            background: "var(--bg-root)", border: "1px dashed var(--border)",
          }}>
            <label style={label}>Import from IdP metadata</label>
            <div style={{ display: "flex", gap: 8 }}>
              <input
                style={{ ...input, flex: 1 }} spellCheck={false}
                placeholder="https://login.microsoftonline.com/<tenant>/federationmetadata/2007-06/federationmetadata.xml"
                value={f.saml.metadata_url}
                onChange={e => setSaml("metadata_url", e.target.value)}
              />
              <Button size="sm" variant="secondary" disabled={importing}
                      onClick={() => runImport({ url: f.saml.metadata_url })}>
                {importing ? "Reading…" : "Import"}
              </Button>
            </div>
            <div style={hint}>
              Fills the Entity ID, sign-in URL and certificate below — nothing is saved
              until you press save. The URL is also kept, so Velaris can re-read it
              periodically and <strong>add a rollover certificate before the old one
              expires</strong>. Only certificates are ever updated that way; a changed
              issuer or sign-in URL is flagged for you, never applied.
            </div>
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
                Paste the XML instead
              </summary>
              <textarea
                style={{ ...input, marginTop: 6, minHeight: 90, fontFamily: "monospace", fontSize: 11 }}
                spellCheck={false} value={metadataXml}
                placeholder="<EntityDescriptor …>"
                onChange={e => setMetadataXml(e.target.value)}
              />
              <Button size="sm" variant="secondary" style={{ marginTop: 6 }} disabled={importing}
                      onClick={() => runImport({ xml: metadataXml })}>
                Import from XML
              </Button>
              <div style={hint}>
                For an IdP this server cannot reach. Nothing is fetched, so no metadata
                URL is stored and there is no automatic certificate renewal.
              </div>
            </details>
            {importMsg && (
              <div style={{ ...hint, color: importMsg.ok ? "var(--status-completed)" : "#dc2626" }}>
                {importMsg.text}
              </div>
            )}
          </div>

          <Field title="IdP Entity ID (Issuer)" help="Must match the Issuer element of every assertion, exactly.">
            <input style={input} value={f.saml.idp_entity_id} spellCheck={false}
                   placeholder="http://www.okta.com/exk1…"
                   onChange={e => setSaml("idp_entity_id", e.target.value)} />
          </Field>

          <Field title="IdP SSO URL" help="Where the browser is sent to sign in (HTTP-Redirect binding).">
            <input style={input} value={f.saml.idp_sso_url} spellCheck={false}
                   placeholder="https://example.okta.com/app/…/sso/saml"
                   onChange={e => setSaml("idp_sso_url", e.target.value)} />
          </Field>

          <Field title="IdP single logout URL (optional)" help="Recorded for the IdP's own logout flow. Velaris does not initiate SLO.">
            <input style={input} value={f.saml.idp_slo_url} spellCheck={false}
                   placeholder="(optional)"
                   onChange={e => setSaml("idp_slo_url", e.target.value)} />
          </Field>

          <ListField
            title="IdP signing certificates"
            values={f.saml.certs}
            onChange={v => setSaml("certs", v)}
            placeholder="MIID… (base64 body or a full PEM block)"
            addLabel="+ Add certificate"
            multiline
            help={<>Any listed certificate verifying is enough, so a second one can be added <em>before</em> the
                  IdP rolls over rather than during the outage. Paste the base64 body or the whole PEM.</>}
          />

          <Field title="SP Entity ID (Audience URI)" help="What you register at the IdP as the audience. An assertion for a different audience is refused.">
            <input style={input} value={f.saml.sp_entity_id} spellCheck={false}
                   placeholder="velaris-studio"
                   onChange={e => setSaml("sp_entity_id", e.target.value)} />
          </Field>

          <div style={row}>
            <label style={label}>ACS URL (Single sign-on URL)</label>
            <div style={{ display: "flex", gap: 8 }}>
              <input style={{ ...input, flex: 1 }} value={f.saml.acs_url} spellCheck={false}
                     placeholder={suggestedAcs}
                     onChange={e => setSaml("acs_url", e.target.value)} />
              <Button size="sm" variant="secondary" onClick={() => setSaml("acs_url", suggestedAcs)}>Use this</Button>
            </div>
            <div style={hint}>
              Configured, never derived from the request: <code>Destination</code> and <code>Recipient</code> are
              compared against this exact string, and deriving it from the <code>Host</code> header would compare an
              attacker-supplied value with itself. The <code>&lt;tenant&gt;</code> segment is load-bearing — it is what
              stops an assertion being replayed into another tenant, and the sign-in button is derived from it.
            </div>
          </div>

          <details>
            <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
              Assertion details
            </summary>
            <div style={{ marginTop: "var(--space-sm)" }}>
              <Field title="Name ID format" help="The NameID is the account's permanent external id — an email-shaped one changes when somebody's address changes.">
                <select style={input} value={f.saml.name_id_format}
                        onChange={e => setSaml("name_id_format", e.target.value)}>
                  {NAME_ID_FORMATS.map(n => <option key={n.value} value={n.value}>{n.label}</option>)}
                </select>
              </Field>

              <Field
                title="Clock skew (seconds)"
                help={`How far the IdP's clock may differ before a fresh assertion looks expired. 0–${MAX_CLOCK_SKEW}: past that it is no longer tolerance, it is accepting expired assertions.`}
              >
                <input style={input} value={f.saml.clock_skew} spellCheck={false} inputMode="numeric"
                       placeholder={DEFAULT_CLOCK_SKEW}
                       onChange={e => setSaml("clock_skew", e.target.value)} />
              </Field>

              <div style={row}>
                <label style={label}>Attribute names</label>
                <div style={{ ...hint, marginTop: 0, marginBottom: 8 }}>
                  Leave blank to use the names Velaris already tries (the standard claim URIs plus each
                  IdP's short forms). Fill one in only when your IdP sends something else. Separate
                  several with commas to try them in order.
                </div>
                {ATTRIBUTE_FIELDS.map(a => (
                  <div key={a.key} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                    <span style={{ fontSize: 12, color: "var(--text-secondary)", width: 96, flexShrink: 0 }}>{a.label}</span>
                    <input
                      style={{ ...input, flex: 1 }} spellCheck={false} placeholder={a.tried}
                      value={f.saml.attributes[a.key] ?? ""}
                      onChange={e => setSaml("attributes", { ...f.saml.attributes, [a.key]: e.target.value })}
                    />
                  </div>
                ))}
              </div>
            </div>
          </details>
        </div>
      )}

      {/* ── allowed domains ── */}
      <ListField
        title="Allowed email domains"
        values={f.domains}
        onChange={v => set("domains", v)}
        placeholder="company.com"
        addLabel="+ Add"
        help={<>
          A user who signs in successfully but has no Velaris account is created only if their
          email domain is listed here. <strong>Leave it empty to switch auto-provisioning off</strong> —
          then SSO only signs in accounts that already exist.
        </>}
      />
      {publicHits.length > 0 && (
        <div style={{ ...warn, marginTop: -6, marginBottom: "var(--space-md)" }}>
          <strong style={{ color: "#dc2626" }}>{publicHits.join(", ")}</strong> {publicHits.length > 1 ? "are" : "is"} a
          public mailbox provider. Anyone in the world can register an address there, so allowlisting
          it grants a Velaris account — with the default roles below — to anyone who asks.
        </div>
      )}

      {/* ── default roles ── */}
      <Field title="Default roles for new users" help="Applied only to accounts created by auto-provisioning. Existing users keep the roles they already have.">
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {ROLES.map(r => (
            <label key={r} style={{
              display: "flex", alignItems: "center", gap: 6, fontSize: 13,
              color: "var(--text-primary)", cursor: "pointer",
            }}>
              <input
                type="checkbox" checked={f.roles.includes(r)}
                onChange={() => set("roles", f.roles.includes(r)
                  ? f.roles.filter(x => x !== r) : [...f.roles, r])}
              />
              {r}
            </label>
          ))}
        </div>
      </Field>

      {/* ── SAML: provisioning switches + landing paths ── */}
      {isSaml && (
        <>
          <Field title="Provisioning">
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-primary)", cursor: "pointer" }}>
              <input type="checkbox" checked={f.saml.auto_provision}
                     onChange={e => setSaml("auto_provision", e.target.checked)} />
              Create an account on first sign-in
            </label>
            <div style={hint}>
              Off, only accounts that already exist can sign in — the same effect as an empty domain
              list, stated separately so a deliberate "existing accounts only" survives someone
              adding a domain.
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-primary)", cursor: "pointer", marginTop: 10 }}>
              <input type="checkbox" checked={f.saml.link_existing_by_email}
                     onChange={e => setSaml("link_existing_by_email", e.target.checked)} />
              Adopt an existing account that matches on email address
            </label>
            {f.saml.link_existing_by_email ? (
              <div style={warn}>
                <strong style={{ color: "#dc2626" }}>Leave this off unless you are mid-migration.</strong>{" "}
                With it on, whoever can set an email attribute in the directory signs in as whichever
                Velaris account already holds that address, inheriting its roles — and the domain
                allowlist does not bite, because the corporate domain is exactly what it allows. Off,
                a taken address is refused and an administrator links the account deliberately on the
                Identity tab.
              </div>
            ) : (
              <div style={hint}>
                Off. An unrecognised NameID provisions a new account; an address already held by
                somebody else is refused, and linked by hand on the Identity tab.
              </div>
            )}
          </Field>

          {/* ── the SP's own key pair ── */}
          <div style={panel}>
            <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>
              This deployment's SAML key
            </div>
            <div style={{ ...hint, marginTop: 0, marginBottom: 10 }}>
              Velaris only needs a key of its own for the three options below. The private
              half is sealed with this tenant's HxVault key and never leaves the server; the
              certificate is public and is what your IdP imports.
            </div>

            {spKey ? (
              <div style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: 10 }}>
                Key in place, expires{" "}
                {spKey.not_after
                  ? new Date(spKey.not_after).toLocaleDateString(undefined,
                      { day: "numeric", month: "short", year: "numeric" })
                  : "—"}
                {spKey.days_remaining !== null && ` (${spKey.days_remaining} days)`}
              </div>
            ) : (
              <div style={{ ...hint, marginTop: 0, marginBottom: 10 }}>
                No key yet. Generate one before switching on request signing, encrypted
                assertions or single logout.
              </div>
            )}

            {editing ? (
              <>
                <Button size="sm" variant="secondary" disabled={keyBusy}
                        onClick={() => runGenerateKey(!!spKey)}>
                  {keyBusy ? "Working…" : spKey ? "Replace key" : "Generate key pair"}
                </Button>
                {spKey && (
                  <div style={{ ...hint }}>
                    Replacing it breaks anything the IdP is already encrypting to the old
                    key, so re-import this SP's metadata there straight afterwards.
                  </div>
                )}
                {keyMsg && (
                  <div style={{ ...hint, color: keyMsg.ok ? "var(--status-completed)" : "#dc2626" }}>
                    {keyMsg.text}
                  </div>
                )}
              </>
            ) : (
              <div style={hint}>Save this provider first, then generate its key here.</div>
            )}

            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13,
                            color: "var(--text-primary)", cursor: "pointer", marginTop: 12 }}>
              <input type="checkbox" checked={f.saml.sign_authn_requests}
                     onChange={e => setSaml("sign_authn_requests", e.target.checked)} />
              Sign the sign-in requests we send
            </label>
            <div style={hint}>
              Off by default — an unsigned request is valid SAML and every IdP tested accepts
              one. Switch it on when the IdP requires it; without a key here, it stays off in
              the published metadata rather than claiming something untrue.
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13,
                            color: "var(--text-primary)", cursor: "pointer", marginTop: 10 }}>
              <input type="checkbox" checked={f.saml.slo_enabled}
                     onChange={e => setSaml("slo_enabled", e.target.checked)} />
              Single logout
            </label>
            <div style={hint}>
              Signing out of Velaris also ends the IdP session, and an IdP-initiated logout
              ends this one. The IdP's signature is what authorises that — an unsigned logout
              request is refused, or anyone could sign a user out at will.
            </div>

            <div style={{ ...hint, marginTop: 8 }}>
              <strong>Encrypted assertions need no switch.</strong> Once a key exists the
              metadata advertises it, and an encrypted assertion is decrypted if the IdP
              chooses to send one.
            </div>
          </div>

          <ListField
            title="Allowed landing paths"
            values={f.saml.relay_prefixes}
            onChange={v => setSaml("relay_prefixes", v)}
            placeholder="/"
            addLabel="+ Add path"
            help={<>Where a completed sign-in may land. RelayState is checked against these prefixes, and
                  <strong> the first one is where a login lands when the IdP sends no RelayState</strong> — a
                  redirect that carries the single-use sign-in code, so it must be a path on this server that
                  the app actually renders.</>}
          />
        </>
      )}

      {/* ── sign-in button branding ── */}
      <div style={panel}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>
          Sign-in button
        </div>
        <div style={{ ...hint, marginTop: 0, marginBottom: 10 }}>
          What the login page shows. Both are public — the sign-in page is, and so is the fact that it
          offers SSO. {isSaml && <>Without a name the button would read “saml”, which tells a user nothing
          about which door they are being sent to.</>}
        </div>
        <Field title="Button label">
          <input style={input} value={f.display_name} spellCheck={false}
                 placeholder={providerLabel(f.provider)}
                 onChange={e => set("display_name", e.target.value)} />
        </Field>
        <div style={{ marginBottom: 4 }}>
          <label style={label}>Icon</label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input style={{ ...input, flex: 1 }} value={f.icon_url} spellCheck={false}
                   placeholder="/idp/okta.svg  or  data:image/svg+xml;base64,…"
                   onChange={e => set("icon_url", e.target.value)} />
            {iconAllowed(f.icon_url) && f.icon_url.trim() && (
              <img src={f.icon_url.trim()} alt="" style={{ width: 22, height: 22, objectFit: "contain" }} />
            )}
          </div>
          <div style={hint}>
            A path served by Velaris (put the file in <code>studio/public/</code>) or a{" "}
            <code>data:</code> URI. A remote URL is refused: fetching it from the login page would tell
            that host who is about to sign in, and would break the moment it does.
          </div>
          {!iconAllowed(f.icon_url) && (
            <div style={warn}>That is a remote URL. The server drops it, so the button would render without an icon.</div>
          )}
        </div>
      </div>

      {/* ── advanced key/value ── */}
      <details style={{ ...row, borderTop: "1px solid var(--border)", paddingTop: "var(--space-md)" }}>
        <summary style={{ cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
          Advanced config ({f.extra.length})
        </summary>
        <div style={{ marginTop: "var(--space-sm)" }}>
          <div style={hint}>
            Any other key stored on this provider's <code>config</code>. Values are parsed as JSON when
            they look like JSON, otherwise kept as text. Everything the fields above manage is written
            from those fields and cannot be set here.
          </div>
          {f.extra.map((kv, i) => (
            <div key={i} style={{ display: "flex", gap: 8, marginTop: 6 }}>
              <input style={{ ...input, flex: 1 }} value={kv.key} placeholder="key" spellCheck={false}
                     onChange={e => set("extra", f.extra.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} />
              <input style={{ ...input, flex: 2 }} value={kv.value} placeholder="value" spellCheck={false}
                     onChange={e => set("extra", f.extra.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} />
              <Button size="sm" variant="ghost"
                      onClick={() => set("extra", f.extra.filter((_, j) => j !== i))}>✕</Button>
            </div>
          ))}
          <Button size="sm" variant="secondary" style={{ marginTop: 8 }}
                  onClick={() => set("extra", [...f.extra, { key: "", value: "" }])}>
            + Add key
          </Button>
        </div>
      </details>

      {editing && (
        <Field title="Status" help="A disabled provider disappears from every sign-in page immediately.">
          <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-primary)", cursor: "pointer" }}>
            <input type="checkbox" checked={f.enabled} onChange={e => set("enabled", e.target.checked)} />
            Enabled
          </label>
        </Field>
      )}

      {/* ── what to register at the IdP ── */}
      <div style={panel}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", marginBottom: 6 }}>
          {isSaml
            ? "Give these to your identity provider"
            : `Register these redirect URIs with ${providerLabel(f.provider) || "your provider"}`}
        </div>
        <div style={{ fontSize: 12, fontFamily: "monospace", color: "var(--text-secondary)", lineHeight: 1.9, overflowWrap: "anywhere" }}>
          {isSaml ? (
            <>
              <div>{f.saml.acs_url || suggestedAcs} <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— Single sign-on URL (ACS)</span></div>
              <div>{f.saml.sp_entity_id || "velaris-studio"} <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— Audience URI</span></div>
              <div>{origin}/api/v1/auth/saml/{urlTenant}/metadata <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— SP metadata XML</span></div>
              <div>{origin}/api/v1/auth/saml/{urlTenant}/login <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— where the button sends the browser</span></div>
            </>
          ) : (
            <>
              <div>{origin}/auth/sso/callback <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— Studio sign-in</span></div>
              <div>{origin}/portal/&lt;slug&gt;/login <span style={{ fontFamily: "inherit", color: "var(--text-muted)" }}>— one per customer portal</span></div>
            </>
          )}
        </div>
        <div style={hint}>
          {isSaml
            ? "The metadata URL is public and carries only what the IdP admin would otherwise type. Paste a captured assertion into the SAML tester on the Identity tab to check the mapping before anyone signs in."
            : "Sign-in fails at the provider until these exactly match what is registered there."}
        </div>
      </div>

      {error && (
        <div style={{ fontSize: 12, color: "#dc2626", marginBottom: "var(--space-sm)" }}>{error}</div>
      )}
      {confirmPublic && (
        <div style={{ fontSize: 12, color: "#dc2626", marginBottom: "var(--space-sm)" }}>
          Save again to confirm you meant to allow a public mailbox domain.
        </div>
      )}

      <div style={{ display: "flex", gap: "var(--space-sm)" }}>
        <Button size="sm" onClick={save} disabled={saving}>
          {saving ? "Saving…" : confirmPublic ? "Save anyway" : editing ? "Save changes" : "Create provider"}
        </Button>
        <Button size="sm" variant="secondary" onClick={onCancel} disabled={saving}>Cancel</Button>
      </div>
    </Card>
  );
}

// ── the tab ───────────────────────────────────────────────────────

/** Which required SAML fields a stored row is missing. Same list the backend
 *  checks, so the card can say "the button will not appear" rather than leaving
 *  the admin to discover it at the login page. */
function samlGaps(cfg: Record<string, any>): string[] {
  const gaps: string[] = [];
  if (!String(cfg.idp_entity_id ?? "").trim()) gaps.push("IdP entity id");
  if (!String(cfg.idp_sso_url ?? "").trim()) gaps.push("IdP SSO URL");
  if (!String(cfg.sp_entity_id ?? "").trim()) gaps.push("SP entity id");
  if (!String(cfg.acs_url ?? "").trim()) gaps.push("ACS URL");
  if (!asList(cfg.idp_x509_certs).length) gaps.push("signing certificate");
  // Shape, not presence: an ACS URL that does not name a tenant is stored
  // happily and then fails `_acs_matches_route` on every assertion.
  if (String(cfg.acs_url ?? "").trim() && !acsTenant(String(cfg.acs_url))) {
    gaps.push("an ACS URL of the form /auth/saml/<tenant>/acs");
  }
  return gaps;
}

/** Certificate expiry and the last metadata refresh, on the provider card.
 *
 *  On the card rather than in a notification on purpose: this is *state*, not an
 *  event. A dismissed alert leaves an expiring certificate invisible; a badge
 *  stays true until somebody fixes it. */
function SamlHealth({ health }: { health: SsoProviderAdmin["saml_health"] }) {
  if (!health) return null;
  const sync = health.metadata_sync;
  const expired = health.certs.filter(c => c.expired);
  const expiring = health.certs.filter(
    c => !c.expired && c.days_remaining !== null && c.days_remaining <= CERT_WARN_DAYS);
  const drift = sync?.drift ? Object.entries(sync.drift) : [];

  const note: React.CSSProperties = {
    ...hint, marginTop: 8, padding: "6px 9px", borderRadius: 5,
    background: "var(--bg-elevated)", border: "1px solid var(--border)",
  };

  // The date itself, always — not only once it is nearly too late. A warning
  // that appears at 30 days is the alarm; this is the thing an admin can check
  // on any ordinary day, and plan a rollover around.
  const dated = health.certs.filter(c => c.not_after);
  const soonest = dated.length
    ? dated.reduce((a, b) => (a.not_after! < b.not_after! ? a : b))
    : null;

  return (
    <>
      {soonest && !soonest.expired && (
        <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4 }}>
          {[
            "Certificate expires ",
            new Date(soonest.not_after!).toLocaleDateString(undefined,
              { day: "numeric", month: "short", year: "numeric" }),
            soonest.days_remaining !== null ? ` (${soonest.days_remaining} days)` : "",
            dated.length > 1 ? ` — soonest of ${dated.length}` : "",
          ].join("")}
        </div>
      )}
      {expired.length > 0 && (
        <div style={{ ...warn, marginTop: 8 }}>
          {expired.length === health.certs.length
            ? "Every signing certificate has expired — sign-in through this provider is failing now."
            : `${expired.length} signing certificate${expired.length === 1 ? " has" : "s have"} expired.`}{" "}
          Import the IdP's current metadata, then remove the dead one.
        </div>
      )}
      {expiring.length > 0 && (
        <div style={{ ...warn, marginTop: 8 }}>
          A signing certificate expires in {Math.min(...expiring.map(c => c.days_remaining!))} days.
          Add the replacement <em>before</em> then — both verify while they overlap, so a rollover
          costs no downtime.
        </div>
      )}
      {drift.length > 0 && (
        <div style={{ ...warn, marginTop: 8 }}>
          The IdP's published metadata no longer matches this provider, and was{" "}
          <strong>not applied</strong>: {drift.map(([k]) => k).join(", ")}. Import it to review
          the change — repointing a live sign-in is never automatic.
        </div>
      )}
      {sync?.last_error && (
        <div style={note}>
          Last metadata refresh failed: {sync.last_error}. The configuration is unchanged and
          sign-in is unaffected.
        </div>
      )}
      {sync?.last_checked && !sync.last_error && !drift.length && (
        <div style={note}>
          Metadata checked {new Date(sync.last_checked).toLocaleString()}.
        </div>
      )}
    </>
  );
}

export default function SsoTab() {
  const { data, loading, refetch } = useApi(listSsoProvidersAdmin);
  const providers = (data ?? []) as SsoProviderAdmin[];
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<SsoProviderAdmin | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const close = useCallback(() => { setCreating(false); setEditing(null); refetch(); }, [refetch]);

  const remove = async (p: SsoProviderAdmin) => {
    if (!window.confirm(`Delete the ${providerLabel(p.provider)} provider? Its button disappears from every sign-in page. Users already linked to it will no longer be able to sign in.`)) return;
    setBusyId(p.id);
    try { await deleteSsoProvider(p.id); refetch(); }
    catch (e: any) { window.alert(e?.message || "Delete failed"); }
    finally { setBusyId(null); }
  };

  return (
    <div>
      <Card style={{ marginBottom: "var(--space-lg)", background: "var(--bg-elevated)" }}>
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
          <strong style={{ color: "var(--text-primary)" }}>SSO providers</strong> add a sign-in button to
          the Studio login page. Google and GitHub are built in; any other OIDC provider works by naming
          it and entering its endpoints. Register the app with the provider first, then enter its client
          id and secret here — there are no environment variables or config files to edit.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>SAML 2.0</strong> is configured here too, under
          the provider name <code>saml</code>: it needs the IdP's entity id, sign-in URL and signing
          certificate instead of a client secret. The SP metadata and the assertion tester are on the
          Identity tab.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Staff sign-in only.</strong> The customer
          portal has its own identity system — customers verify by email code — so SSO here never
          affects them.
          <br />
          <strong style={{ color: "var(--text-primary)" }}>Auto-provisioning</strong> is off unless you list
          allowed email domains: without them, SSO signs in existing accounts only. A user whose provider
          has not verified their email address is always refused, and disabled accounts cannot sign in
          through SSO any more than through a password.
        </div>
      </Card>

      {!creating && !editing && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
          <span style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            {providers.length} provider{providers.length !== 1 ? "s" : ""}
          </span>
          <Button size="sm" onClick={() => setCreating(true)}>+ New Provider</Button>
        </div>
      )}

      {(creating || editing) && (
        <ProviderForm editing={editing} onDone={close} onCancel={close} />
      )}

      {loading && <Spinner size={28} />}
      {!loading && !providers.length && !creating && (
        <EmptyState
          title="No SSO providers"
          description="Add a Google, GitHub or SAML provider to offer single sign-on on the login page."
        />
      )}

      {providers.map(p => {
        const cfg = p.config ?? {};
        const isSamlRow = p.provider === SAML;
        const domains = asList(cfg[domainKey(p.provider)]);
        const roles = asList(cfg.default_roles);
        const autoProvision = isSamlRow ? cfg.auto_provision !== false : true;
        const gaps = isSamlRow ? samlGaps(cfg) : [];
        const brand = String(cfg.display_name ?? "").trim();
        return (
          <Card key={p.id} style={{ marginBottom: "var(--space-sm)", opacity: busyId === p.id ? 0.5 : 1 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "var(--space-md)" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "var(--space-sm)", flexWrap: "wrap" }}>
                  <span style={{
                    width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                    background: p.enabled ? "var(--status-completed)" : "var(--text-muted)",
                  }} />
                  <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>
                    {brand || providerLabel(p.provider)}
                  </span>
                  {brand && (
                    <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{providerLabel(p.provider)}</span>
                  )}
                  {!p.enabled && <span style={{ fontSize: 11, color: "var(--text-muted)" }}>disabled</span>}
                  {!isSamlRow && !p.has_secret && <span style={{ fontSize: 11, color: "#dc2626" }}>no client secret</span>}
                  {isSamlRow && cfg.link_existing_by_email === true && (
                    <span style={{ fontSize: 11, color: "#dc2626" }}>adopts accounts by email</span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6, fontFamily: "monospace", overflowWrap: "anywhere" }}>
                  {isSamlRow ? (String(cfg.idp_entity_id ?? "") || "— no IdP entity id") : p.client_id}
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6, lineHeight: 1.7 }}>
                  <div>
                    Auto-provisioning:{" "}
                    {!autoProvision
                      ? <strong style={{ color: "var(--text-primary)" }}>off — auto-provisioning disabled</strong>
                      : domains.length
                        ? <><strong style={{ color: "var(--text-primary)" }}>{domains.join(", ")}</strong> → {(roles.length ? roles : ["viewer"]).join(", ")}</>
                        : <strong style={{ color: "var(--text-primary)" }}>off — existing accounts only</strong>}
                  </div>
                  <div>Tenant: {p.tenant_id ?? "all tenants"}</div>
                </div>
                {gaps.length > 0 && (
                  <div style={{ ...warn, marginTop: 8 }}>
                    Incomplete, so sign-in answers “SAML is not configured here” and no button appears.
                    Missing: {gaps.join(", ")}.
                  </div>
                )}
                {isSamlRow && <SamlHealth health={p.saml_health} />}
              </div>
              <div style={{ display: "flex", gap: "var(--space-sm)", flexShrink: 0 }}>
                <Button size="sm" variant="secondary" onClick={() => { setCreating(false); setEditing(p); }}>Edit</Button>
                <Button size="sm" variant="danger" onClick={() => remove(p)}>Delete</Button>
              </div>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
