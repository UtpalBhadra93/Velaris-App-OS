/**
 * Admin Console → SSO.
 *
 * The screen's job is to make two security decisions hard to get wrong by
 * accident: an empty domain allowlist means auto-provisioning is OFF (so the
 * field must never look optional or incidental), and a public mailbox domain
 * grants an account to anyone who can register an address there.
 *
 * The secret handling is the other load-bearing behaviour. Velaris never sends
 * a stored client secret back, so the edit form's blank field means "keep the
 * one you have" — if that were ever submitted as an empty secret it would
 * silently break a working provider.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Typed loosely on purpose: the assertions below index into mock.calls, which
// a zero-arg vi.fn() types as an empty tuple.
type AnyMock = (...args: any[]) => any;
const listSsoProvidersAdmin = vi.fn<AnyMock>();
const createSsoProvider = vi.fn<AnyMock>(async () => ({}));
const updateSsoProvider = vi.fn<AnyMock>(async () => ({}));
const deleteSsoProvider = vi.fn<AnyMock>(async () => undefined);
const parseSamlMetadata = vi.fn<AnyMock>();

vi.mock("@shared/api/client", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("@shared/api/client");
  return {
    ...actual,
    listSsoProvidersAdmin: (...a: any[]) => listSsoProvidersAdmin(...a),
    createSsoProvider: (...a: any[]) => createSsoProvider(...a),
    updateSsoProvider: (...a: any[]) => updateSsoProvider(...a),
    deleteSsoProvider: (...a: any[]) => deleteSsoProvider(...a),
    parseSamlMetadata: (...a: any[]) => parseSamlMetadata(...a),
  };
});

import SsoTab from "./SsoTab";

const GOOGLE = {
  id: "p1", provider: "google", client_id: "cid.apps.googleusercontent.com",
  enabled: true, tenant_id: null, has_secret: true, created_at: null,
  config: { allowed_domains: ["company.com"], default_roles: ["viewer"] },
};

beforeEach(() => {
  vi.clearAllMocks();
  listSsoProvidersAdmin.mockResolvedValue([]);
});

async function open(providers: unknown[] = []) {
  listSsoProvidersAdmin.mockResolvedValue(providers);
  const user = userEvent.setup();
  render(<SsoTab />);
  await waitFor(() => expect(listSsoProvidersAdmin).toHaveBeenCalled());
  return user;
}

describe("the provisioning summary", () => {
  it("says provisioning is off when no domains are allowed", async () => {
    await open([{ ...GOOGLE, config: { default_roles: ["viewer"] } }]);
    // The explainer card says the same thing in prose, so match the card's
    // own summary line exactly rather than the substring.
    await waitFor(() => expect(screen.getByText("off — existing accounts only")).toBeInTheDocument());
  });

  it("names the domains and roles when it is on", async () => {
    await open([GOOGLE]);
    await waitFor(() => expect(screen.getByText("company.com")).toBeInTheDocument());
  });
});

describe("the public-domain guardrail", () => {
  it("warns, and does not save on the first attempt", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));

    await user.type(screen.getByPlaceholderText(/apps\.googleusercontent\.com/i), "cid");
    await user.type(screen.getByPlaceholderText("company.com"), "gmail.com");
    // Password inputs have no implicit role, so reach for it directly.
    const secret = document.querySelector('input[type="password"]') as HTMLInputElement;
    await user.type(secret, "shh");

    expect(screen.getByText(/public mailbox provider/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /create provider/i }));
    expect(createSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/save again to confirm/i)).toBeInTheDocument();
  });

  it("lets a deliberate admin through on the second attempt", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    await user.type(screen.getByPlaceholderText(/apps\.googleusercontent\.com/i), "cid");
    await user.type(screen.getByPlaceholderText("company.com"), "gmail.com");
    await user.type(document.querySelector('input[type="password"]') as HTMLInputElement, "shh");

    await user.click(screen.getByRole("button", { name: /create provider/i }));
    await user.click(screen.getByRole("button", { name: /save anyway/i }));

    await waitFor(() => expect(createSsoProvider).toHaveBeenCalled());
    expect(createSsoProvider.mock.calls[0][0].config.allowed_domains).toEqual(["gmail.com"]);
  });
});

describe("editing an existing provider", () => {
  it("omits the secret entirely when the field is left blank", async () => {
    const user = await open([GOOGLE]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    const [id, body] = updateSsoProvider.mock.calls[0];
    expect(id).toBe("p1");
    expect(body).not.toHaveProperty("client_secret");
  });

  it("sends the secret only when one is typed", async () => {
    const user = await open([GOOGLE]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    await user.type(document.querySelector('input[type="password"]') as HTMLInputElement, "rotated");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].client_secret).toBe("rotated");
  });

  it("normalises domains and drops blank rows", async () => {
    const user = await open([GOOGLE]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    const domain = screen.getByDisplayValue("company.com");
    await user.clear(domain);
    await user.type(domain, "  @Company.COM ");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].config.allowed_domains).toEqual(["company.com"]);
  });

  it("will not let the provider be switched under an existing secret", async () => {
    const user = await open([GOOGLE]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    expect(screen.getByRole("combobox")).toBeDisabled();
  });
});

describe("a custom (non-preset) provider", () => {
  async function newProvider(name: string) {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    const providerInput = screen.getByPlaceholderText(/google, github, saml/i);
    await user.clear(providerInput);
    await user.type(providerInput, name);
    await user.type(screen.getByPlaceholderText(/client id from your identity provider/i), "cid");
    await user.type(document.querySelector('input[type="password"]') as HTMLInputElement, "shh");
    await user.type(screen.getByPlaceholderText("company.com"), "company.com");
    return user;
  }

  it("hides the endpoint fields for a built-in provider", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    expect(screen.queryByText(/OAuth endpoints for/i)).toBeNull();
  });

  it("asks for endpoints as soon as the name is not built in", async () => {
    await newProvider("okta");
    expect(screen.getByText(/OAuth endpoints for/i)).toBeInTheDocument();
  });

  it("refuses to save without the required endpoints", async () => {
    const user = await newProvider("okta");
    await user.click(screen.getByRole("button", { name: /create provider/i }));
    expect(createSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/is not a built-in provider/i)).toBeInTheDocument();
  });

  it("refuses a plaintext http endpoint", async () => {
    const user = await newProvider("okta");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/authorize/), "https://x.okta.com/authorize");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/token/), "http://x.okta.com/token");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/userinfo/), "https://x.okta.com/userinfo");
    await user.type(screen.getByPlaceholderText("openid email profile"), "openid email");

    await user.click(screen.getByRole("button", { name: /create provider/i }));
    expect(createSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/must use https/i)).toBeInTheDocument();
  });

  it("sends the endpoints in config when they are all present", async () => {
    const user = await newProvider("okta");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/authorize/), "https://x.okta.com/authorize");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/token/), "https://x.okta.com/token");
    await user.type(screen.getByPlaceholderText(/oauth2\/v1\/userinfo/), "https://x.okta.com/userinfo");
    await user.type(screen.getByPlaceholderText("openid email profile"), "openid email");

    await user.click(screen.getByRole("button", { name: /create provider/i }));
    await waitFor(() => expect(createSsoProvider).toHaveBeenCalled());
    const body = createSsoProvider.mock.calls[0][0];
    expect(body.provider).toBe("okta");
    expect(body.config).toMatchObject({
      auth_url: "https://x.okta.com/authorize",
      token_url: "https://x.okta.com/token",
      userinfo: "https://x.okta.com/userinfo",
      scope: "openid email",
    });
  });

  it("never writes endpoint keys for a preset, which the backend ignores anyway", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    await user.type(screen.getByPlaceholderText(/apps\.googleusercontent\.com/i), "cid");
    await user.type(document.querySelector('input[type="password"]') as HTMLInputElement, "shh");
    await user.type(screen.getByPlaceholderText("company.com"), "company.com");
    await user.click(screen.getByRole("button", { name: /create provider/i }));

    await waitFor(() => expect(createSsoProvider).toHaveBeenCalled());
    expect(createSsoProvider.mock.calls[0][0].config).not.toHaveProperty("token_url");
  });
});

describe("what the admin is told to register at the IdP", () => {
  it("shows both redirect URIs", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    expect(screen.getByText(/\/auth\/sso\/callback/)).toBeInTheDocument();
    expect(screen.getByText(/\/portal\/<slug>\/login/)).toBeInTheDocument();
  });
});

/**
 * SAML rows live in the same table but read a different allowlist key, so the
 * screen used to summarise a working provider as "off — existing accounts
 * only" and would have written the OIDC key over the top of the real one.
 */
describe("a SAML provider", () => {
  const SAML_ROW = {
    id: "s1", provider: "saml", client_id: "okta-saml", enabled: true,
    tenant_id: "dev", has_secret: true, created_at: null,
    config: {
      idp_entity_id: "http://www.okta.com/exk1",
      idp_sso_url: "https://trial.okta.com/app/x/sso/saml",
      idp_x509_certs: ["MIIDqjCCApKg"],
      sp_entity_id: "velaris-studio",
      acs_url: "http://localhost:5173/api/v1/auth/saml/dev/acs",
      email_domain_allowlist: ["company.com"],
      default_roles: ["viewer"],
      allowed_relay_state_prefixes: ["/"],
      auto_provision: true,
      link_existing_by_email: false,
      display_name: "Okta",
    },
  };

  const editSaml = async (config: Record<string, unknown> = {}) => {
    const user = await open([{ ...SAML_ROW, config: { ...SAML_ROW.config, ...config } }]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    return user;
  };

  it("reads the SAML allowlist key, not the OIDC one", async () => {
    await open([SAML_ROW]);
    await waitFor(() => expect(screen.getByText("company.com")).toBeInTheDocument());
    expect(screen.queryByText("off — existing accounts only")).toBeNull();
  });

  it("still says off when auto-provisioning is switched off despite a domain", async () => {
    await open([{ ...SAML_ROW, config: { ...SAML_ROW.config, auto_provision: false } }]);
    await waitFor(() => expect(screen.getByText(/auto-provisioning disabled/i)).toBeInTheDocument());
  });

  it("flags a row the login path would refuse", async () => {
    await open([{ ...SAML_ROW, config: { ...SAML_ROW.config, idp_x509_certs: [] } }]);
    const flag = await screen.findByText(/Incomplete/i);
    expect(flag.parentElement).toHaveTextContent(/signing certificate/);
  });

  it("offers no client secret — there is none to rotate", async () => {
    await editSaml();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("round-trips every SAML key it renders, and never writes allowed_domains", async () => {
    const user = await editSaml();
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    const body = updateSsoProvider.mock.calls[0][1];
    expect(body.config).not.toHaveProperty("allowed_domains");
    expect(body.config).toMatchObject({
      idp_entity_id: "http://www.okta.com/exk1",
      idp_sso_url: "https://trial.okta.com/app/x/sso/saml",
      idp_x509_certs: ["MIIDqjCCApKg"],
      sp_entity_id: "velaris-studio",
      acs_url: "http://localhost:5173/api/v1/auth/saml/dev/acs",
      email_domain_allowlist: ["company.com"],
      allowed_relay_state_prefixes: ["/"],
      auto_provision: true,
      link_existing_by_email: false,
      display_name: "Okta",
    });
    // The client id is not the SAML row's own field; an edit leaves it alone.
    expect(body.client_id).toBe("okta-saml");
  });

  it("does not turn auto-provisioning off on a row that never stored the key", async () => {
    const row = { ...SAML_ROW, config: { ...SAML_ROW.config } };
    delete (row.config as any).auto_provision;
    const user = await open([row]);
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].config.auto_provision).toBe(true);
  });

  it("refuses an ACS URL that names no tenant", async () => {
    const user = await editSaml();
    const acs = screen.getByDisplayValue(SAML_ROW.config.acs_url);
    await user.clear(acs);
    await user.type(acs, "https://velaris.example.com/acs");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/must end in \/auth\/saml\/<tenant>\/acs/i)).toBeInTheDocument();
  });

  it("refuses an ACS URL whose tenant disagrees with the provider's", async () => {
    const user = await editSaml();
    const acs = screen.getByDisplayValue(SAML_ROW.config.acs_url);
    await user.clear(acs);
    await user.type(acs, "https://velaris.example.com/api/v1/auth/saml/other/acs");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/names tenant "other"/i)).toBeInTheDocument();
  });

  /* The first landing path is the redirect target when the IdP sends no
   * RelayState, and that redirect carries a live sign-in code — so a value a
   * browser reads as another host hands the code to that host. A leading
   * slash is not the test: `\` normalises to `/` in an http(s) URL. */
  it.each([
    ["an absolute URL", "https://evil.example.com"],
    ["a scheme-relative path", "//evil.example.com"],
    ["a backslash path", "/\\evil.example.com"],
    ["a relative path", "studio"],
  ])("refuses %s as a landing path", async (_name, value) => {
    const user = await editSaml();
    const prefix = screen.getByDisplayValue("/");
    await user.clear(prefix);
    await user.type(prefix, value);
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/is not a path on this server/i)).toBeInTheDocument();
  });

  it("refuses a clock skew that would switch the assertion's expiry off", async () => {
    const user = await editSaml({ clock_skew_seconds: 120 });
    await user.click(screen.getByText(/assertion details/i));
    const skew = screen.getByDisplayValue("120");
    await user.clear(skew);
    await user.type(skew, "999999");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/between 0 and 600/i)).toBeInTheDocument();
  });

  it("refuses a plaintext IdP sign-in URL", async () => {
    const user = await editSaml();
    const sso = screen.getByDisplayValue(SAML_ROW.config.idp_sso_url);
    await user.clear(sso);
    await user.type(sso, "http://trial.okta.com/app/x/sso/saml");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/must use https/i)).toBeInTheDocument();
  });

  it("does not let the advanced grid overwrite a validated field", async () => {
    const user = await editSaml();
    await user.click(screen.getByText(/advanced config/i));
    await user.click(screen.getByRole("button", { name: /add key/i }));
    await user.type(screen.getByPlaceholderText("key"), "acs_url");
    await user.type(screen.getByPlaceholderText("value"), "https://evil.example.com/acs");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].config.acs_url).toBe(SAML_ROW.config.acs_url);
  });

  it("keeps an unknown key the grid does own", async () => {
    const user = await editSaml({ some_future_key: "kept" });
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].config.some_future_key).toBe("kept");
  });

  it("warns while account adoption by email is on", async () => {
    await editSaml({ link_existing_by_email: true });
    expect(screen.getByText(/unless you are mid-migration/i)).toBeInTheDocument();
  });

  /* The login page is public, so an icon fetched from a third party reports
   * every visitor to that host before anyone has authenticated. */
  it.each([
    "https://cdn.example.com/okta.svg",
    "//cdn.example.com/okta.svg",
    "/\\cdn.example.com/okta.svg",
  ])("refuses the off-origin icon %s", async (value) => {
    const user = await editSaml();
    const icon = screen.getByPlaceholderText(/idp\/okta\.svg/i);
    await user.type(icon, value);
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    expect(updateSsoProvider).not.toHaveBeenCalled();
    expect(screen.getByText(/icon must be a path on this server/i)).toBeInTheDocument();
  });

  it("accepts an icon path that merely contains a hyphen", async () => {
    const user = await editSaml();
    const icon = screen.getByPlaceholderText(/idp\/okta\.svg/i);
    await user.type(icon, "/idp/okta-lockup.svg");
    await user.click(screen.getByRole("button", { name: /save changes/i }));

    await waitFor(() => expect(updateSsoProvider).toHaveBeenCalled());
    expect(updateSsoProvider.mock.calls[0][1].config.icon_url).toBe("/idp/okta-lockup.svg");
  });

  it("shows the IdP what to register, not an OAuth redirect URI", async () => {
    await editSaml();
    expect(screen.getByText(/\/metadata/)).toBeInTheDocument();
    expect(screen.queryByText(/\/auth\/sso\/callback/)).toBeNull();
  });
});

/**
 * Importing metadata is the setup shortcut: every IdP names these fields
 * differently, and the metadata document is the one place they all agree. It
 * fills the form and saves nothing — an import that wrote straight through
 * would let one fetch repoint a live sign-in.
 */
describe("importing IdP metadata", () => {
  const META = {
    idp_entity_id: "https://sts.windows.net/tenant-guid/",
    idp_sso_url: "https://login.microsoftonline.com/tenant-guid/saml2",
    idp_slo_url: "https://login.microsoftonline.com/tenant-guid/saml2",
    idp_x509_certs: ["MIICimported"],
    name_id_format: null,
    certs: [{ fingerprint: "MIICimported", not_after: "2029-01-01T00:00:00+00:00",
              days_remaining: 900, expired: false }],
    source: "url" as const,
  };

  async function openSamlForm() {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    const prov = screen.getByPlaceholderText(/google, github, saml/i);
    await user.clear(prov);
    await user.type(prov, "saml");
    return user;
  }

  it("fills the fields the metadata carries", async () => {
    parseSamlMetadata.mockResolvedValue(META);
    const user = await openSamlForm();
    await user.type(screen.getByPlaceholderText(/federationmetadata/i), "https://idp.test/meta");
    await user.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(parseSamlMetadata).toHaveBeenCalledWith({ url: "https://idp.test/meta" }));
    expect(await screen.findByDisplayValue(META.idp_entity_id)).toBeInTheDocument();
    // Entra publishes the same location for sign-in and logout, so both fields
    // legitimately hold it.
    expect(screen.getAllByDisplayValue(META.idp_sso_url)).toHaveLength(2);
    expect(screen.getByDisplayValue("MIICimported")).toBeInTheDocument();
    expect(screen.getByText(/imported 1 certificate/i)).toBeInTheDocument();
  });

  it("saves nothing on its own", async () => {
    parseSamlMetadata.mockResolvedValue(META);
    const user = await openSamlForm();
    await user.type(screen.getByPlaceholderText(/federationmetadata/i), "https://idp.test/meta");
    await user.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(parseSamlMetadata).toHaveBeenCalled());
    expect(createSsoProvider).not.toHaveBeenCalled();
    expect(updateSsoProvider).not.toHaveBeenCalled();
  });

  it("reports a refusal instead of half-filling the form", async () => {
    parseSamlMetadata.mockRejectedValue(new Error("That URL is not allowed: private address"));
    const user = await openSamlForm();
    await user.type(screen.getByPlaceholderText(/federationmetadata/i), "https://127.0.0.1/meta");
    await user.click(screen.getByRole("button", { name: /^import$/i }));

    expect(await screen.findByText(/not allowed: private address/i)).toBeInTheDocument();
  });
});

describe("the SAML health badges", () => {
  const base = {
    id: "s9", provider: "saml", client_id: "x", enabled: true, tenant_id: "acme",
    has_secret: true, created_at: null,
    config: { idp_entity_id: "e", idp_sso_url: "s", idp_x509_certs: ["c"],
              sp_entity_id: "sp", acs_url: "https://v.test/api/v1/auth/saml/acme/acs",
              email_domain_allowlist: ["acme.com"], default_roles: ["viewer"] },
  };
  const health = (h: any) => [{ ...base, saml_health: h }];

  it("shows the expiry date on an ordinary day, not only when warning", async () => {
    await open(health({ certs: [{ fingerprint: "c", not_after: "2029-08-17T11:32:01Z",
                                  days_remaining: 1095, expired: false }], metadata_sync: null }));
    // Locale-independent: the runner formats dates as en-US, a browser elsewhere
    // will not, and the date format is not what this test is about.
    expect(await screen.findByText(/Certificate expires .*2029.* \(1095 days\)/)).toBeInTheDocument();
  });

  it("names the soonest when several are configured", async () => {
    await open(health({ certs: [
      { fingerprint: "a", not_after: "2030-01-01T00:00:00Z", days_remaining: 1200, expired: false },
      { fingerprint: "b", not_after: "2027-03-04T00:00:00Z", days_remaining: 200, expired: false },
    ], metadata_sync: null }));
    expect(await screen.findByText(/2027.* \(200 days\) — soonest of 2/)).toBeInTheDocument();
  });

  it("warns before a certificate expires, not after", async () => {
    await open(health({ certs: [{ fingerprint: "c", not_after: "2026-09-01T00:00:00Z",
                                  days_remaining: 12, expired: false }], metadata_sync: null }));
    expect(await screen.findByText(/expires in 12 days/i)).toBeInTheDocument();
  });

  it("says plainly when every certificate has expired", async () => {
    await open(health({ certs: [{ fingerprint: "c", not_after: "2026-01-01T00:00:00Z",
                                  days_remaining: -30, expired: true }], metadata_sync: null }));
    expect(await screen.findByText(/sign-in through this provider is failing now/i)).toBeInTheDocument();
  });

  it("shows a failed refresh without implying sign-in broke", async () => {
    await open(health({ certs: [], metadata_sync: { last_error: "That URL answered 404." } }));
    expect(await screen.findByText(/sign-in is unaffected/i)).toBeInTheDocument();
  });

  it("says drift was not applied", async () => {
    await open(health({ certs: [], metadata_sync: {
      last_checked: "2026-08-17T00:00:00Z",
      drift: { idp_sso_url: { configured: "a", published: "b" } } } }));
    expect(await screen.findByText(/not applied/i)).toBeInTheDocument();
  });
});

describe("superadmin", () => {
  it("is not offered as a default role", async () => {
    const user = await open();
    await user.click(screen.getByRole("button", { name: /new provider/i }));
    const roles = screen.getByText(/default roles for new users/i).parentElement!;
    expect(within(roles).queryByText("superadmin")).toBeNull();
    expect(within(roles).getByText("viewer")).toBeInTheDocument();
  });
});
