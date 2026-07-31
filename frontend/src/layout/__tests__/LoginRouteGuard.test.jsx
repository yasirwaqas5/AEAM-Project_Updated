import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "../AuthProvider";
import { ToastProvider } from "../ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * The /login route guard.
 *
 * `/login` was the ONLY route in App.jsx's table with no guard on it, so it
 * rendered the sign-in form unconditionally — including for a caller who
 * already held a valid session. In a development posture that is a dead end
 * you cannot wait out: AuthProvider auto-acquires a dev token seconds after
 * load, so `isAuthenticated` flips true, yet the page kept asking for a
 * bearer token nobody has to paste.
 *
 * The realistic way in is transient: uvicorn --reload restarts the backend
 * constantly during development, one boot's dev-token fetch fails,
 * RequireAuth redirects to /login, and the URL then STAYS there forever
 * because nothing sends a now-authenticated caller back.
 *
 * These tests pin BOTH directions of the guard — that a dev session is let
 * through, and that a production posture (dev-token 404s) still shows the
 * form exactly as it did before.
 * ────────────────────────────────────────────────────────────────────────── */

function b64url(obj) {
  return btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function fakeJwt(payload) {
  return `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.sig`;
}

/** The guard under test — mirrors App.jsx's RedirectIfAuthenticated. */
function RedirectIfAuthenticated({ children }) {
  const { isAuthenticated, booting } = useAuth();
  const location = useLocation();
  if (booting) return <div data-testid="booting">booting</div>;
  if (isAuthenticated) {
    const target = location.state?.from?.pathname || "/";
    return <Navigate to={target === "/login" ? "/" : target} replace />;
  }
  return children;
}

function Harness({ initialEntries }) {
  return (
    <MemoryRouter initialEntries={initialEntries}>
      <ToastProvider>
        <AuthProvider>
          <Routes>
            <Route path="/" element={<div data-testid="app">Dashboard</div>} />
            <Route path="/incidents" element={<div data-testid="incidents">Incidents</div>} />
            <Route
              path="/login"
              element={
                <RedirectIfAuthenticated>
                  <div data-testid="login-form">Sign in to continue</div>
                </RedirectIfAuthenticated>
              }
            />
          </Routes>
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>
  );
}

/** Backend in a development posture: dev-token mints a session. */
function mockDevBackend() {
  vi.spyOn(window, "fetch").mockImplementation(async (url) => {
    const u = String(url);
    if (u.includes("/api/v1/auth/dev-token")) {
      return {
        ok: true,
        json: async () => ({
          access_token: fakeJwt({
            sub: "dev-user", roles: ["admin"], exp: Math.floor(Date.now() / 1000) + 3600,
          }),
          token_type: "bearer",
        }),
      };
    }
    if (u.includes("/api/v1/auth/sso/config")) {
      return { ok: true, json: async () => ({ enabled: false, reason: "OIDC_ENABLED is false." }) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
}

/** Backend in a production posture: dev-token is gated off and 404s. */
function mockProdBackend() {
  vi.spyOn(window, "fetch").mockImplementation(async (url) => {
    const u = String(url);
    if (u.includes("/api/v1/auth/dev-token")) {
      return { ok: false, status: 404, json: async () => ({ detail: "Not found." }) };
    }
    if (u.includes("/api/v1/auth/sso/config")) {
      return { ok: true, json: async () => ({ enabled: false, reason: "OIDC_ENABLED is false." }) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
}

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("/login route guard", () => {
  it("development: a caller landing on /login is taken into the app, not asked to sign in", async () => {
    mockDevBackend();
    render(<Harness initialEntries={["/login"]} />);

    await waitFor(() => expect(screen.getByTestId("app")).toBeTruthy());
    expect(screen.queryByTestId("login-form")).toBeNull();
  });

  it("development: resumes the deep link that bounced through /login", async () => {
    mockDevBackend();
    render(
      <Harness initialEntries={[{ pathname: "/login", state: { from: { pathname: "/incidents" } } }]} />
    );

    // RequireAuth records `from` when it redirects; honouring it means a
    // deep link survives the bounce instead of dumping the user on the home page.
    await waitFor(() => expect(screen.getByTestId("incidents")).toBeTruthy());
    expect(screen.queryByTestId("login-form")).toBeNull();
  });

  it("production: the sign-in form still renders when no session can be acquired", async () => {
    mockProdBackend();
    render(<Harness initialEntries={["/login"]} />);

    await waitFor(() => expect(screen.getByTestId("login-form")).toBeTruthy());
    expect(screen.queryByTestId("app")).toBeNull();
  });

  it("production: a previously pasted, still-valid token is let through", async () => {
    mockProdBackend();
    sessionStorage.setItem(
      "aeam.auth.token",
      fakeJwt({ sub: "analyst", roles: ["analyst"], exp: Math.floor(Date.now() / 1000) + 3600 })
    );
    render(<Harness initialEntries={["/login"]} />);

    await waitFor(() => expect(screen.getByTestId("app")).toBeTruthy());
    expect(screen.queryByTestId("login-form")).toBeNull();
  });

  it("production: an EXPIRED stored token still shows the form (guard must not trust storage alone)", async () => {
    mockProdBackend();
    sessionStorage.setItem(
      "aeam.auth.token",
      fakeJwt({ sub: "analyst", roles: ["analyst"], exp: Math.floor(Date.now() / 1000) - 60 })
    );
    render(<Harness initialEntries={["/login"]} />);

    await waitFor(() => expect(screen.getByTestId("login-form")).toBeTruthy());
    expect(screen.queryByTestId("app")).toBeNull();
  });

  it("never redirects /login back to /login (no loop)", async () => {
    mockDevBackend();
    render(
      <Harness initialEntries={[{ pathname: "/login", state: { from: { pathname: "/login" } } }]} />
    );

    await waitFor(() => expect(screen.getByTestId("app")).toBeTruthy());
    expect(screen.queryByTestId("login-form")).toBeNull();
  });
});
