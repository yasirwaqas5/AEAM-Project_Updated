# AEAM Console (frontend)

React + Vite console for AEAM. Phase E10 made this console enterprise-deployable:
real authentication/session handling, role-aware navigation, production
serving from the same process as the API, and a test baseline.

## Local development

```bash
npm install
npm run dev
```

Vite proxies `/api`, `/health`, and `/metrics` to `http://localhost:8080` (see
`vite.config.js`). Run the backend with `ENVIRONMENT=development` and the
console logs itself in automatically (see **Authentication** below) — no
extra setup needed for local work.

## Production build & serving

```bash
npm run build
```

This produces `frontend/dist/`. **AEAM serves the console from the same
FastAPI process it serves the API from** (`aeam/main.py`'s
`_mount_frontend_build`) — there is no separate frontend server or reverse
proxy step required. If `frontend/dist` exists when the backend starts, it
is mounted automatically:

- `GET /assets/*` serves the built JS/CSS bundles.
- Any other `GET` route that isn't an API/infra path (`/api/*`, `/health`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`) falls back to
  `index.html`, so client-side routing (react-router) works on a hard
  refresh/deep link.
- If `frontend/dist` does not exist, the mount is a silent no-op and
  `GET /` keeps returning the pre-existing liveness JSON — local dev
  (`npm run dev` on 5173/5174) is completely unaffected.

Run `npm run build` before deploying; the backend does not build it for you.

## Authentication & sessions

AEAM validates tokens issued elsewhere — it is not an identity provider.
Real SSO/OIDC federation is planned for a later phase and will attach to
this same session layer (`layout/AuthProvider.jsx`).

- **Development** (`ENVIRONMENT=development` on the backend):
  `AuthProvider` calls the dev-only `POST /api/v1/auth/dev-token` endpoint
  at boot and logs itself in as `admin` automatically. This endpoint 404s
  in every other environment, so it can never reach staging/production.
  This preserves the pre-E10 zero-friction local workflow.
- **Staging/production**: that endpoint 404s, so the console shows
  `pages/Login.jsx` — paste a bearer token issued by your organization's
  identity provider. The token is decoded client-side (never verified —
  verification only ever happens server-side, in `SecurityMiddleware`) to
  drive role-aware navigation, and is held in `sessionStorage` (cleared
  when the tab closes).
- Every same-origin API call automatically gets `Authorization: Bearer
  <token>` attached by a one-time `fetch` wrap in `lib/api.js` — no
  individual page had to be changed for this. A `401` response anywhere
  logs the session out and redirects to `/login` with an honest "session
  expired" toast; pages never silently render as if nothing happened.

## Role-aware navigation (RBAC lockstep)

`lib/rbac.js` mirrors `aeam/security/rbac.py`'s permission matrix — same
five roles (`analyst`, `operator`, `admin`, `auditor`, `readonly`), same
`resource:action` grant vocabulary. **These two files must be kept in sync
by hand** — same lockstep discipline as `deriveStatus` in `components/ui.jsx`
against `aeam/agents/orchestrator/investigation_status.py`. If the backend
matrix changes, update `lib/rbac.js` in the same change.

`config/nav.js` tags each route with the `permission` it requires.
`layout/Sidebar.jsx` hides items the current session doesn't grant;
`App.jsx`'s `RequirePermission` enforces the same check at the route level,
so a direct URL to a hidden page renders a 403 page, not the page itself.
This is a UI courtesy, not a security boundary — `SecurityMiddleware` is
the only real enforcement, and always wins.

## Tests

```bash
npm test
```

Runs the Vitest suite (`jsdom` environment, React Testing Library):

- `src/components/__tests__/ui.deriveStatus.test.jsx` — the `deriveStatus`
  lockstep against the backend's 5-state investigation-status vocabulary.
- `src/lib/__tests__/rbac.test.js` — the RBAC permission-matrix lockstep.
- `src/layout/__tests__/AuthProvider.test.jsx` — session boot (dev
  auto-login vs. manual login), token decoding, expiry rejection, logout.
