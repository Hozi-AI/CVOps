# Edge auth: nginx + oauth2-proxy + Keycloak

Opt-in authentication at the edge. nginx serves the whole app and asks
**oauth2-proxy** whether each request has a valid session. oauth2-proxy runs
the OIDC login against an identity provider (**Keycloak** in dev; any OIDC IdP
works). The API never sees a password or a token, only the identity nginx
forwards.

With auth off (the default) none of this is loaded, and the stack behaves
exactly as before.

```
Browser ─► nginx :80 ──auth_request /oauth2/auth──► oauth2-proxy :4180 ──OIDC──► Keycloak :8081
              │  202 → forward X-Auth-Request-Email/User + X-Proxy-Secret
              ├─► /api/v1/*, /dataset → API   (no session: API 401 → SPA → /oauth2/start)
              └─► /                   → Vite  (no session: 302 → /oauth2/start → Keycloak)
```

Not gated: workers call the API directly (`/api/v1/internal/*`, `WORKER_TOKEN`),
`/health` isn't proxied, and Garage presigned URLs go straight to Garage.

## Turning it on

1. Fill the **Edge auth** block of `manifests/.env` (see `.env.example`):
   `PROXY_SHARED_SECRET`, `OAUTH2_PROXY_COOKIE_SECRET`, `OAUTH2_PROXY_CLIENT_SECRET`,
   `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_SEED_USER_PASSWORD`. VM devs also set
   `KEYCLOAK_PUBLIC_URL=http://<dev-vm>:8081`.
2. Start the stack:
   - `tilt up -- --auth` (or `CVOPS_ENABLE_AUTH=1`), or
   - `cd manifests && docker compose -f docker-compose.yml -f docker-compose.auth.yml up`
3. Open `http://localhost`. You're sent to Keycloak; log in as
   `alice@example.com` or `bob@example.com` with `KEYCLOAK_SEED_USER_PASSWORD`.
   On first login the API creates the CVOps user with a personal org, in which
   they're the owner.

Keycloak admin console: `http://localhost:8081` (`admin` / `KEYCLOAK_ADMIN_PASSWORD`).

## How the pieces fit

| Piece | File | Role |
|---|---|---|
| Include hooks | `manifests/nginx/nginx.conf` | `include .../cvops-auth-*.inc` globs. They match nothing unless the overlay is on. |
| Auth snippets | `manifests/nginx/auth-templates/*.template` | Rendered into `conf.d/` by the nginx image's envsubst step. `server` holds the `/oauth2/*` locations, `api` the gate that returns a plain 401, `page` the gate that redirects to login. |
| Overlay | `manifests/docker-compose.auth.yml` | Mounts the templates into nginx and adds the `keycloak` + `oauth2-proxy` services. |
| Realm | `manifests/keycloak/realm-cvops.json` | Realm `cvops`, confidential client `cvops-edge`, and two seed users. `${...}` resolves from the container env. |
| API | `services/api/src/cvops_api/core/proxy_auth.py` | With `AUTH_MODE=proxy`, `get_current_user` trusts `X-Auth-Request-Email` **only** when `X-Proxy-Secret` matches, and provisions the user on first login. |
| SPA | `services/frontend/src/lib/edgeAuth.ts` | With `VITE_AUTH_MODE=proxy`: no login page, a 401 goes to `/oauth2/start`, and logout goes to `/oauth2/sign_out`. |

### Why the shared secret

In Tilt the API listens on host `:8000`, which you can reach without going
through nginx. A bare `X-Auth-Request-Email` header would let anyone claim any
identity. nginx overwrites the identity headers on every gated request and adds
`X-Proxy-Secret`, and the API rejects any request without it. The API refuses
to start in proxy mode with a secret shorter than 32 characters.

### Why OIDC discovery is skipped

The browser reaches Keycloak at `KEYCLOAK_PUBLIC_URL` (`localhost:8081`), but
oauth2-proxy reaches it at `keycloak:8080`. Keycloak pins `iss` to the public
URL (`KC_HOSTNAME`) while still answering on the internal hostname
(`KC_HOSTNAME_BACKCHANNEL_DYNAMIC`). oauth2-proxy is therefore given the
browser-facing `login_url` and the internal `redeem_url`/`jwks_url` explicitly.

## Swapping the IdP

Only the `oauth2-proxy` environment in `docker-compose.auth.yml` changes:
provider, client id and secret, and the issuer/login/redeem/JWKS URLs. You can
then drop `keycloak`. nginx, the API and the SPA don't care which IdP is used.

## Not done yet

- Mapping IdP groups to CVOps orgs/roles. Every new user currently gets a
  personal org; see `docs/15-keycloak-user-management.md`.
- TLS at the edge. Once nginx terminates HTTPS, set `OAUTH2_PROXY_COOKIE_SECURE=true`.
- The compose `frontend` container is built without `VITE_AUTH_MODE`. The
  edge proxies `/` to the host Vite dev server, which Tilt configures.
