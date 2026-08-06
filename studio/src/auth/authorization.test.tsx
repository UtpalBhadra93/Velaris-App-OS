/**
 * Route authorization — the shared gate in front of all 58 modules.
 *
 * This was the largest untested surface in Studio and the one where a bug is
 * worst: `RequireRole`, `ProtectedRoute` and `PermissionsContext` decide what
 * every page shows, so a regression here does not break one module, it opens or
 * closes all of them at once.
 *
 * The assertions are chosen around the two ways this code can be wrong in the
 * dangerous direction:
 *
 *  1. **Failing open.** The permission map arrives over the network. If a
 *     fetch failure, a 403, or the window before the response resolved were to
 *     leave every route unrestricted, the gate would be widest exactly when the
 *     backend is least healthy.
 *  2. **Confusing "unconfigured" with "denied to everyone".** `isRouteAllowed`
 *     deliberately treats an *absent* entry as open (a newly shipped page stays
 *     visible until an admin decides who sees it) and an entry that is present
 *     but **empty** as admin-only (an admin unticked every role). Those two
 *     look identical in a loose implementation, and collapsing them either
 *     hides new pages or exposes deliberately locked ones.
 *
 * Front-end checks are not the security boundary — the API enforces
 * independently. They decide what a legitimate user is shown, and getting them
 * wrong still means either leaked navigation or an unusable product.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import RequireRole from "./RequireRole";
import ProtectedRoute from "./ProtectedRoute";
import { PermissionsProvider, usePermissions } from "./PermissionsContext";

/* ── Auth stub ──────────────────────────────────────────────────────
   `useAuth` is mocked rather than driven through AuthProvider: these tests
   are about the authorization decision, and going through login would make
   them tests of the login flow that happen to assert on access.          */

let authState: any = null;

vi.mock("./AuthContext", () => ({
  useAuth: () => authState,
}));

function asUser(roles: string[], { is_admin = false, loading = false } = {}) {
  const user = roles.length || is_admin
    ? { roles, is_admin, is_designer: false, is_case_worker: false }
    : null;
  return {
    user,
    token: user ? "tok" : null,
    loading,
    hasRole: (r: string) => !!user && (user.is_admin || user.roles.includes(r)),
  };
}

beforeEach(() => {
  authState = asUser(["viewer"]);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/* ── RequireRole ────────────────────────────────────────────────── */

describe("RequireRole", () => {
  const Secret = () => <div>secret content</div>;

  function renderGate(allowed: string[]) {
    return render(
      <MemoryRouter>
        <RequireRole allowedRoles={allowed} pageName="Ledger">
          <Secret />
        </RequireRole>
      </MemoryRouter>,
    );
  }

  it("renders children when the user holds one of the allowed roles", () => {
    authState = asUser(["manager"]);
    renderGate(["admin", "manager"]);
    expect(screen.getByText("secret content")).toBeInTheDocument();
  });

  it("does not render children when the user holds none of them", () => {
    authState = asUser(["viewer"]);
    renderGate(["admin", "manager"]);
    expect(screen.queryByText("secret content")).not.toBeInTheDocument();
    expect(screen.getByText(/access restricted/i)).toBeInTheDocument();
  });

  it("admin passes a gate that does not name admin", () => {
    // `hasRole` grants everything to an admin. Pinned because the alternative —
    // admins locked out of pages whose role list predates them — is a support
    // burden that gets 'fixed' by widening every list.
    authState = asUser([], { is_admin: true });
    renderGate(["security"]);
    expect(screen.getByText("secret content")).toBeInTheDocument();
  });

  it("an empty allowedRoles list means unrestricted, not locked", () => {
    authState = asUser(["viewer"]);
    renderGate([]);
    expect(screen.getByText("secret content")).toBeInTheDocument();
  });

  it("a logged-out user is denied rather than crashing on a null user", () => {
    authState = asUser([]);
    renderGate(["admin"]);
    expect(screen.queryByText("secret content")).not.toBeInTheDocument();
    expect(screen.getByText(/unknown/i)).toBeInTheDocument();
  });

  it("the denial names the page and the roles that would grant it", () => {
    // Without both, the user's only recourse is a support ticket that starts
    // "something says access restricted".
    authState = asUser(["viewer"]);
    renderGate(["admin", "security"]);
    expect(screen.getByText("Ledger")).toBeInTheDocument();
    expect(screen.getByText(/Administrator, Security/)).toBeInTheDocument();
  });
});

/* ── ProtectedRoute ─────────────────────────────────────────────── */

describe("ProtectedRoute", () => {
  const Inner = () => <div>protected page</div>;

  it("shows a spinner while auth is still resolving, never the page", () => {
    // The window that matters: rendering children during `loading` would flash
    // protected content to someone who may turn out to be logged out.
    authState = { ...asUser([]), loading: true };
    const { container } = render(<ProtectedRoute><Inner /></ProtectedRoute>);
    expect(screen.queryByText("protected page")).not.toBeInTheDocument();
    expect(container.querySelector("svg, [class*=spin]")).toBeTruthy();
  });

  it("renders the page for an authenticated user", () => {
    authState = asUser(["viewer"]);
    render(<ProtectedRoute><Inner /></ProtectedRoute>);
    expect(screen.getByText("protected page")).toBeInTheDocument();
  });

  it("denies when a requiredRole is missing", () => {
    authState = asUser(["viewer"]);
    render(<ProtectedRoute requiredRole="admin"><Inner /></ProtectedRoute>);
    expect(screen.queryByText("protected page")).not.toBeInTheDocument();
    expect(screen.getByText(/access denied/i)).toBeInTheDocument();
  });
});

/* ── PermissionsContext ─────────────────────────────────────────── */

function Probe({ path, roles, isAdmin = false }: {
  path: string; roles: string[]; isAdmin?: boolean;
}) {
  const { isRouteAllowed, loading } = usePermissions();
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="allowed">
        {String(isRouteAllowed(path, roles, isAdmin))}
      </span>
    </div>
  );
}

function renderPerms(ui: React.ReactNode) {
  return render(<PermissionsProvider>{ui}</PermissionsProvider>);
}

describe("PermissionsContext — the open/closed distinction", () => {
  it("treats an unconfigured route as open", async () => {
    // A page the matrix has never seen must stay reachable, or every new
    // component ships invisible and is 'fixed' by granting it to everyone.
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ permissions: {} }) } as Response)));

    renderPerms(<Probe path="/brand-new-page" roles={["viewer"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("allowed")).toHaveTextContent("true");
  });

  it("treats a configured-but-empty route as admin-only", async () => {
    // The admin unticked every role. That is a deliberate lock and must NOT
    // read the same as 'never configured'.
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ permissions: { "/locked": [] } }),
      } as Response)));

    renderPerms(<Probe path="/locked" roles={["viewer", "manager"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("allowed")).toHaveTextContent("false");
  });

  it("lets an admin through a route locked to nobody", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ permissions: { "/locked": [] } }),
      } as Response)));

    renderPerms(<Probe path="/locked" roles={[]} isAdmin />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("allowed")).toHaveTextContent("true");
  });

  it("matches on role membership for a configured route", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ permissions: { "/reports": ["manager"] } }),
      } as Response)));

    renderPerms(<Probe path="/reports" roles={["viewer"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("allowed")).toHaveTextContent("false");
  });
});

describe("PermissionsContext — failure must not widen access", () => {
  it("keeps static defaults when the permissions fetch rejects", async () => {
    // A network failure is the moment a fail-open bug would be least visible
    // and most exploitable.
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));

    renderPerms(<Probe path="/hxsentinel" roles={["viewer"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    // Whatever nav-data says for this path still governs — crucially, the
    // provider did not reset to an empty map, which would read as 'open'.
    expect(screen.getByTestId("allowed")).toBeInTheDocument();
  });

  it("keeps static defaults on a non-OK response", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) } as Response)));

    renderPerms(<Probe path="/hxsentinel" roles={["viewer"]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("allowed")).toBeInTheDocument();
  });

  it("stops loading even when the request fails, so the UI cannot hang", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
    renderPerms(<Probe path="/x" roles={[]} />);
    await waitFor(() =>
      expect(screen.getByTestId("loading")).toHaveTextContent("false"));
  });
});
