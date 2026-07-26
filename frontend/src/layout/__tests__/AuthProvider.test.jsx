import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider, useAuth } from "../AuthProvider";
import { ToastProvider } from "../ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E10: AuthProvider is the console's session layer. These tests cover
 * the contract that matters most to the acceptance criteria:
 *   - token expiry produces an honest state change (never silent empty data)
 *   - a dev posture auto-acquires a session with zero user action
 *   - a pasted token is decoded and exposed as roles/sub for role-aware UI
 * No real backend runs here -- fetch is mocked per test.
 * ────────────────────────────────────────────────────────────────────────── */

function b64url(obj) {
  return btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fakeJwt(payload) {
  return `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.sig`;
}

function Probe() {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="booting">{String(auth.booting)}</span>
      <span data-testid="authed">{String(auth.isAuthenticated)}</span>
      <span data-testid="sub">{auth.sub || ""}</span>
      <span data-testid="roles">{(auth.roles || []).join(",")}</span>
      <span data-testid="dev">{String(auth.isDev)}</span>
      <button onClick={() => auth.login(window.__testToken)}>login</button>
      <button onClick={() => auth.logout()}>logout</button>
    </div>
  );
}

function renderWithProviders() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <ToastProvider>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  delete window.__testToken;
});

describe("AuthProvider boot sequence", () => {
  it("auto-authenticates as admin when the backend is in a development posture", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (url) => {
      if (String(url).includes("/api/v1/auth/dev-token")) {
        return {
          ok: true,
          json: async () => ({
            access_token: fakeJwt({ sub: "dev-user", roles: ["admin"], exp: Math.floor(Date.now() / 1000) + 3600 }),
            token_type: "bearer",
          }),
        };
      }
      return { ok: false, status: 404 };
    });

    renderWithProviders();

    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));
    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(screen.getByTestId("sub").textContent).toBe("dev-user");
    expect(screen.getByTestId("roles").textContent).toBe("admin");
    expect(screen.getByTestId("dev").textContent).toBe("true");
  });

  it("requires a manual login when the dev-token probe 404s (staging/production)", async () => {
    vi.spyOn(window, "fetch").mockResolvedValue({ ok: false, status: 404 });

    renderWithProviders();

    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));
    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("dev").textContent).toBe("false");
  });
});

describe("AuthProvider.login", () => {
  beforeEach(() => {
    vi.spyOn(window, "fetch").mockResolvedValue({ ok: false, status: 404 });
  });

  it("decodes a pasted token's claims and exposes roles/sub", async () => {
    window.__testToken = fakeJwt({ sub: "alice", roles: ["analyst"], exp: Math.floor(Date.now() / 1000) + 3600 });
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));

    act(() => screen.getByText("login").click());

    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(screen.getByTestId("sub").textContent).toBe("alice");
    expect(screen.getByTestId("roles").textContent).toBe("analyst");
  });

  it("rejects an already-expired token rather than silently accepting it", async () => {
    window.__testToken = fakeJwt({ sub: "bob", roles: ["operator"], exp: Math.floor(Date.now() / 1000) - 10 });
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));

    act(() => screen.getByText("login").click());

    expect(screen.getByTestId("authed").textContent).toBe("false");
  });

  it("logout clears the session", async () => {
    window.__testToken = fakeJwt({ sub: "carol", roles: ["readonly"], exp: Math.floor(Date.now() / 1000) + 3600 });
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));

    act(() => screen.getByText("login").click());
    expect(screen.getByTestId("authed").textContent).toBe("true");

    act(() => screen.getByText("logout").click());
    expect(screen.getByTestId("authed").textContent).toBe("false");
  });
});
