# Keycloak (login server)

Keycloak is the identity provider of the auth plan in
[`15-keycloak-user-management.md`](15-keycloak-user-management.md) (epic #164).
This page covers the infrastructure (#165): running it, the committed realm, and
Sign in with Google / GitHub. The API and frontend start using it in #166 and #168;
until then Keycloak runs alongside the app's existing login.

## Turning it on

Every login setting must be chosen explicitly. There are no defaults, and both
Tilt and the `keycloak` container refuse to start until these keys in
`manifests/.env` are filled in. `.env.example` explains each one.

| Key | Meaning |
|---|---|
| `CVOPS_ENABLE_KEYCLOAK` | `true` starts Keycloak with `tilt up`, `false` doesn't |
| `KEYCLOAK_PUBLIC_URL` | where the browser reaches Keycloak, e.g. `http://localhost:8081` |
| `CVOPS_APP_ORIGIN` | where the browser reaches CVOps, e.g. `http://localhost` |
| `GOOGLE_LOGIN_ENABLED` / `GITHUB_LOGIN_ENABLED` | `true` shows the provider's button, `false` hides it |

Then run either:

- `tilt up` (with `CVOPS_ENABLE_KEYCLOAK=true`), or
- `cd manifests && docker compose --profile auth up keycloak`

On the first start Keycloak imports `manifests/keycloak/realm-cvops.json` into its
own `keycloak` database on the shared postgres. `keycloak-db-init` creates that
database idempotently. Later starts reuse the stored realm, so users and sessions
survive restarts. The image runs as UID 1000 (non-root).

- Admin console: `http://localhost:8081/admin` (`admin` / `KEYCLOAK_ADMIN_PASSWORD`)
- Seed users: `alice@example.com` (owner) and `bob@example.com` (editor) in the
  demo org, password `KEYCLOAK_SEED_USER_PASSWORD`

## The realm (`manifests/keycloak/realm-cvops.json`)

| Piece | What it is |
|---|---|
| `cvops-frontend` | Public client for the dashboard: Authorization Code + PKCE (S256) only. Redirects only to `CVOPS_APP_ORIGIN/` and `http://localhost:5173/`. Adds the `cvops-api` audience and the `org_id` claim to access tokens. |
| `cvops-api` | Bearer-only client: the API's audience as a resource server. |
| `cvops-worker` | Confidential service-account client (`KEYCLOAK_WORKER_CLIENT_SECRET`) for worker → API calls. |
| Roles | Realm roles `owner`, `editor`, `viewer`. |
| `demo-org` group | One group per org, with attribute `org_id`. Its subgroups `owners` / `editors` / `viewers` carry the matching role, so membership implies the role. |
| Google / GitHub | Identity providers, each enabled by its `*_LOGIN_ENABLED` key. |

An access token for alice therefore carries `aud: cvops-api`,
`org_id: 8d6f3c1e-…` and `realm_access.roles: [owner]`.

### Why the redirect URIs are exact

After login Keycloak sends the browser back to the client's redirect URI with a
one-time code that can be exchanged for tokens. A wildcard (`*`) would let a
crafted login link send that code to any site. Keycloak would redirect to
`https://evil.example/?code=…`, and the attacker could use the code. The realm
lists only the app origin and the Vite dev server, so any other `redirect_uri` is
rejected. #168's keycloak-js setup must therefore use
`redirectUri: window.location.origin + '/'` and carry deep links in `state`.

## Sign in with Google / GitHub

Each provider needs an OAuth app. `.env.example` explains `*_CLIENT_ID` (the app's
public identifier), `*_CLIENT_SECRET` (its password, never committed or shared)
and `*_LOGIN_ENABLED`.

| Provider | Create it at | Callback / redirect URL |
|---|---|---|
| Google | console.cloud.google.com → APIs & Services → Credentials → OAuth client ID → Web application | `<KEYCLOAK_PUBLIC_URL>/realms/cvops/broker/google/endpoint` |
| GitHub | Settings → Developer settings → OAuth Apps → New OAuth App (use the Hozi-AI org for team apps) | `<KEYCLOAK_PUBLIC_URL>/realms/cvops/broker/github/endpoint` |

Google: on the consent screen choose *External* and add yourself as a test user.
Google accepts `http://localhost` but not a bare IP, so VM devs need a hostname.

With a provider on, anyone with such an account can sign in. Restricting who may
sign in is part of the RBAC and org-mapping work in #72 and #61.

## Reset

The realm file and its env values are read only when the realm is first created.
After changing them (for example enabling Google), drop Keycloak's database. This
deletes only Keycloak's own users and sessions. CVOps data is untouched.

```bash
cd manifests
docker compose stop keycloak
docker compose exec postgres psql -U cvops -d postgres -c "DROP DATABASE keycloak"
docker compose --profile auth up -d keycloak     # or re-trigger it in Tilt
```
