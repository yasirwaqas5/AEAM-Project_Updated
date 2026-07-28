import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider, useAuth } from "../AuthProvider";
import { ToastProvider } from "../ToastHost";

/* ──────────────────────────────────────────────────────────────────────────
 * Phase E13 — the console's SSO half of enterprise federation.
 *
 * What matters here is the contract the backend and the IdP see:
 *   - the authorization redirect carries PKCE (S256), a CSRF state, and the
 *     registered client id / redirect URI
 *   - the callback exchanges the code exactly once, verifies state, and
 *     installs the resulting token through the normal session path
 *   - every failure produces a real reason, never a silent dead end
 *   - a deployment without SSO is unchanged (the E10 paste-token path)
 *
 * No backend and no IdP run here: fetch, WebCrypto and window.location are
 * stubbed, so the code under test executes its real PKCE, state and
 * exchange logic.
 * ────────────────────────────────────────────────────────────────────────── */

function b64url(obj) {
  return btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fakeJwt(payload) {
  return `${b64url({ alg: "RS256", typ: "JWT" })}.${b64url(payload)}.sig`;
}

const IDP_TOKEN = () =>
  fakeJwt({ sub: "alice@example.com", roles: ["analyst"], exp: Math.floor(Date.now() / 1000) + 3600 });

const SSO_CONFIG = {
  enabled: true,
  issuer: "https://idp.test.example.com",
  client_id: "aeam-console",
  authorization_endpoint: "https://idp.test.example.com/authorize",
  redirect_uri: "https://aeam.example.com/auth/callback",
  scopes: "openid profile email",
  response_type: "code",
  code_challenge_method: "S256",
};

let assigned = [];
let callbackBodies = [];

function Probe({ search = "" }) {
  const auth = useAuth();
  return (
    <div>
      <span data-testid="booting">{String(auth.booting)}</span>
      <span data-testid="authed">{String(auth.isAuthenticated)}</span>
      <span data-testid="sub">{auth.sub || ""}</span>
      <span data-testid="ssoEnabled">{String(auth.ssoEnabled)}</span>
      <span data-testid="ssoReason">{auth.sso?.reason || ""}</span>
      <span data-testid="result" />
      <button onClick={async () => {
        const r = await auth.loginWithSso();
        document.querySelector('[data-testid="result"]').textContent = r.ok ? "ok" : r.error;
      }}>sso</button>
      <button onClick={async () => {
        const r = await auth.completeSsoLogin(search);
        document.querySelector('[data-testid="result"]').textContent = r.ok ? "ok" : r.error;
      }}>callback</button>
    </div>
  );
}

function renderWithProviders(search = "") {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <ToastProvider>
        <AuthProvider>
          <Probe search={search} />
        </AuthProvider>
      </ToastProvider>
    </MemoryRouter>
  );
}

/** Backend stub: SSO enabled/disabled, dev-token always 404 (staging posture). */
function stubBackend({ ssoConfig = SSO_CONFIG, callback = null, callbackStatus = 200 } = {}) {
  vi.spyOn(window, "fetch").mockImplementation(async (url, options) => {
    const u = String(url);
    if (u.includes("/api/v1/auth/sso/config")) {
      return { ok: true, json: async () => ssoConfig };
    }
    if (u.includes("/api/v1/auth/sso/callback")) {
      callbackBodies.push(JSON.parse(options.body));
      return {
        ok: callbackStatus < 400,
        status: callbackStatus,
        json: async () => callback ?? { access_token: IDP_TOKEN(), token_type: "Bearer" },
      };
    }
    return { ok: false, status: 404 };
  });
}

beforeEach(() => {
  sessionStorage.clear();
  assigned = [];
  callbackBodies = [];

  // jsdom's window.location is not assignable; a stub records where the
  // browser would have gone so the redirect URL can be asserted.
  delete window.location;
  window.location = { origin: "https://aeam.example.com", assign: (u) => assigned.push(u) };

  // `globalThis.crypto` is getter-only in this environment, so the whole
  // object is stubbed rather than patched. Randomness is made
  // deterministic and the digest is a fixed 32 bytes — the PKCE code path
  // still runs for real (base64url encoding, verifier/challenge split),
  // only its entropy is pinned so assertions are stable.
  useCrypto({ withSubtle: true });
});

/** Install a stub WebCrypto. `withSubtle: false` simulates a browser that
 *  exposes no SubtleCrypto (e.g. an insecure origin), which must block the
 *  redirect rather than silently downgrade PKCE. */
function useCrypto({ withSubtle }) {
  vi.stubGlobal("crypto", {
    getRandomValues: (arr) => {
      for (let i = 0; i < arr.length; i += 1) arr[i] = (i * 7 + 13) % 256;
      return arr;
    },
    subtle: withSubtle ? { digest: async () => new Uint8Array(32).fill(9).buffer } : undefined,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("SSO configuration probe", () => {
  it("reports SSO as available when the deployment federates identity", async () => {
    stubBackend();
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));
  });

  it("reports SSO as unavailable WITH the reason, never as a silent absence", async () => {
    stubBackend({ ssoConfig: { enabled: false, reason: "OIDC_ENABLED is false for this deployment." } });
    renderWithProviders();

    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));
    expect(screen.getByTestId("ssoEnabled").textContent).toBe("false");
    expect(screen.getByTestId("ssoReason").textContent).toContain("OIDC_ENABLED");
  });

  it("states an honest reason when AEAM itself is unreachable", async () => {
    vi.spyOn(window, "fetch").mockRejectedValue(new Error("network down"));
    renderWithProviders();

    await waitFor(() => expect(screen.getByTestId("ssoReason").textContent).toContain("unreachable"));
    expect(screen.getByTestId("ssoEnabled").textContent).toBe("false");
  });
});

describe("SSO authorization redirect", () => {
  it("redirects to the IdP with PKCE, state, client id and redirect URI", async () => {
    stubBackend();
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));

    await act(async () => screen.getByText("sso").click());

    expect(assigned).toHaveLength(1);
    const url = new URL(assigned[0]);
    expect(`${url.origin}${url.pathname}`).toBe("https://idp.test.example.com/authorize");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("client_id")).toBe("aeam-console");
    expect(url.searchParams.get("redirect_uri")).toBe("https://aeam.example.com/auth/callback");
    expect(url.searchParams.get("scope")).toBe("openid profile email");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBeTruthy();
    expect(url.searchParams.get("state")).toBeTruthy();

    // The verifier must survive the navigation, and must never be the
    // challenge that was sent.
    const verifier = sessionStorage.getItem("aeam.auth.pkce_verifier");
    expect(verifier).toBeTruthy();
    expect(verifier).not.toBe(url.searchParams.get("code_challenge"));
  });

  it("refuses to start and explains why when SSO is not configured", async () => {
    stubBackend({ ssoConfig: { enabled: false, reason: "OIDC_ISSUER is not configured." } });
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("booting").textContent).toBe("false"));

    await act(async () => screen.getByText("sso").click());

    expect(assigned).toHaveLength(0);
    expect(screen.getByTestId("result").textContent).toContain("OIDC_ISSUER");
  });

  it("refuses to start when WebCrypto cannot produce an S256 challenge", async () => {
    useCrypto({ withSubtle: false });
    stubBackend();
    renderWithProviders();
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));

    await act(async () => screen.getByText("sso").click());

    expect(assigned).toHaveLength(0);
    expect(screen.getByTestId("result").textContent).toContain("WebCrypto");
  });
});

describe("SSO callback exchange", () => {
  async function primeRedirect(search) {
    stubBackend();
    const view = renderWithProviders(search);
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));
    await act(async () => screen.getByText("sso").click());
    return view;
  }

  it("exchanges the code and establishes the session", async () => {
    await primeRedirect("");
    const realState = sessionStorage.getItem("aeam.auth.oidc_state");

    // Re-render with the IdP's actual redirect query, reusing this tab's
    // stored verifier/state exactly as a real browser navigation would.
    const verifier = sessionStorage.getItem("aeam.auth.pkce_verifier");
    document.body.innerHTML = "";
    sessionStorage.setItem("aeam.auth.pkce_verifier", verifier);
    sessionStorage.setItem("aeam.auth.oidc_state", realState);

    renderWithProviders(`?code=auth-code-1&state=${realState}`);
    await waitFor(() => expect(screen.getAllByTestId("ssoEnabled")[0].textContent).toBe("true"));

    await act(async () => screen.getAllByText("callback")[0].click());

    await waitFor(() => expect(screen.getAllByTestId("authed")[0].textContent).toBe("true"));
    expect(screen.getAllByTestId("sub")[0].textContent).toBe("alice@example.com");

    const sent = callbackBodies.at(-1);
    expect(sent.code).toBe("auth-code-1");
    expect(sent.code_verifier).toBe(verifier);

    // Single-use: nothing is left behind for a replay.
    expect(sessionStorage.getItem("aeam.auth.pkce_verifier")).toBeNull();
    expect(sessionStorage.getItem("aeam.auth.oidc_state")).toBeNull();
  });

  it("rejects a callback whose state does not match the one it issued", async () => {
    await primeRedirect("?code=auth-code-1&state=forged-state");

    await act(async () => screen.getByText("callback").click());

    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("result").textContent).toContain("state did not match");
    expect(callbackBodies).toHaveLength(0);
  });

  it("surfaces an error the IdP returned instead of a blank screen", async () => {
    await primeRedirect("?error=access_denied&error_description=User+cancelled+the+sign-in");

    await act(async () => screen.getByText("callback").click());

    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("result").textContent).toBe("User cancelled the sign-in");
  });

  it("surfaces the backend's reason when the exchange is rejected", async () => {
    vi.restoreAllMocks();
    stubBackend({ callback: { detail: "Authorization code exchange rejected by the IdP." }, callbackStatus: 400 });

    renderWithProviders("?code=stale-code");
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));

    await act(async () => screen.getByText("callback").click());

    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("result").textContent).toContain("rejected by the IdP");
  });

  it("rejects an already-expired token rather than establishing a dead session", async () => {
    vi.restoreAllMocks();
    stubBackend({
      callback: { access_token: fakeJwt({ sub: "bob", roles: [], exp: Math.floor(Date.now() / 1000) - 30 }) },
    });

    renderWithProviders("?code=c");
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));

    await act(async () => screen.getByText("callback").click());

    expect(screen.getByTestId("authed").textContent).toBe("false");
    expect(screen.getByTestId("result").textContent).toContain("expired");
  });

  it("reports a missing authorization code honestly", async () => {
    stubBackend();
    renderWithProviders("?state=abc");
    await waitFor(() => expect(screen.getByTestId("ssoEnabled").textContent).toBe("true"));

    await act(async () => screen.getByText("callback").click());

    expect(screen.getByTestId("result").textContent).toContain("no authorization code");
  });
});

describe("backward compatibility (E10 posture preserved)", () => {
  it("a deployment without SSO still signs in with a pasted token", async () => {
    stubBackend({ ssoConfig: { enabled: false, reason: "OIDC_ENABLED is false for this deployment." } });

    function PasteProbe() {
      const auth = useAuth();
      return (
        <div>
          <span data-testid="authed">{String(auth.isAuthenticated)}</span>
          <span data-testid="sub">{auth.sub || ""}</span>
          <button onClick={() => auth.login(fakeJwt({ sub: "carol", roles: ["operator"], exp: Math.floor(Date.now() / 1000) + 600 }))}>
            paste
          </button>
        </div>
      );
    }

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <ToastProvider>
          <AuthProvider><PasteProbe /></AuthProvider>
        </ToastProvider>
      </MemoryRouter>
    );

    await act(async () => screen.getByText("paste").click());

    expect(screen.getByTestId("authed").textContent).toBe("true");
    expect(screen.getByTestId("sub").textContent).toBe("carol");
  });
});
