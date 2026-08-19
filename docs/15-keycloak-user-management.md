# Plan: Keycloak as User-Management / IdP

Status: **proposed** · Owner: @YehudaBriskman · Target branch base: `dev`

Replace CVOps' homegrown auth (bcrypt + HS256 JWT + Redis JTI blacklist) with
**Keycloak** as the identity provider. Keycloak owns users, credentials, tokens,
groups, and roles; the API becomes an OIDC **resource server** that validates
RS256 tokens against Keycloak's JWKS and JIT-mirrors identities locally.

## Decisions (locked)

| Fork | Choice |
|---|---|
| Tenancy mapping | **Single realm + Keycloak Groups.** One realm `cvops`; each CVOps Org = a KC Group; org membership role = group role mapping. |
| Frontend flow | **Authorization Code + PKCE** redirect to Keycloak's hosted login (public SPA client). |
| Existing users | **Greenfield / dev reset.** Pre-prod data is disposable; bootstrap a realm with seed users via realm import. No bcrypt migration. |

## What this resolves

- **#72 RBAC** — roles come from Keycloak (group/realm roles → `owner`/`editor`/`viewer`), enforced by a real `core/rbac.py` dependency reading token claims.
- **#61 Org members UI** — invite/role management becomes KC group membership; UI either calls KC Admin API via a thin API proxy or KC Account console.
- **#68 login/register validation** — the custom login/register forms go away (KC-hosted login); registration becomes a KC flow.
- **#30 `/internal/*` auth** — worker auth moves to a KC **service account** (client-credentials) *or* stays on `WORKER_TOKEN`; either way `verify_worker` is added. (Independent of KC; flagged here because it lives in the same auth surface.)

---

## Target architecture

```
Browser ──(Auth Code + PKCE)──► Keycloak (realm: cvops)
   │  access_token (RS256, JWKS)        │ issues tokens, owns users/groups/roles
   ▼                                     ▼
nginx ──► FastAPI resource server ──► verify RS256 via cached JWKS
                                     ──► extract sub, email, org (group), roles
                                     ──► JIT-provision/sync local User/Org/Membership
                                     ──► org_id from claim → existing query-level tenancy
Worker ──(client-credentials)──► Keycloak ──► service-account token ──► /internal/*
```

Local Postgres keeps `users`/`orgs`/`memberships` as a **mirror** (FKs across the
app point at `users.id`/`orgs.id`, so we cannot drop them). They are populated
from token claims, not from `/register`.

### Token validation (replaces HS256 path)

- `core/auth.py`: swap `jwt.decode(..., JWT_SECRET, HS256)` for RS256 verification
  against Keycloak's JWKS (`/realms/cvops/protocol/openid-connect/certs`), with
  `aud`/`iss` checks and a cached, auto-refreshing key set (`python-jose` can
  verify; fetch JWKS with a small cached client, refresh on `kid` miss).
- `sub` becomes the **Keycloak user id** (not our UUID) → add `User.kc_sub` and
  resolve users by it.
- Redis JTI blacklist is **dropped** for access tokens (KC short-lived access +
  refresh handles revocation); optionally keep Redis for KC **back-channel logout**
  if we wire it later.

### Identity mirroring (JIT provisioning)

On each validated token, ensure local rows exist:
- `Org` keyed by group id claim (`org_id` mapped into the token), created if absent.
- `User` keyed by `kc_sub`, email from claim, created/updated if absent.
- `Membership` role from the realm/group role claim.

This keeps the existing `current_user.org_id` query-level multi-tenancy untouched —
the only change is *where org_id comes from*.

---

## Keycloak realm design

- **Realm:** `cvops`.
- **Clients:**
  - `cvops-frontend` — public, standard flow (Auth Code + PKCE), redirect URIs for `http://localhost`, `:5173`, dev VM.
  - `cvops-api` — bearer-only (resource server, audience).
  - `cvops-worker` — confidential, service-accounts enabled (client-credentials for `/internal/*`).
- **Groups:** one per Org; group attribute `org_id` (UUID) mapped into tokens via a protocol mapper.
- **Roles:** realm or client roles `owner`, `editor`, `viewer`; assigned at group level so membership implies role.
- **Protocol mappers:** put `org_id` and `roles` into the access token so the API reads them without an Admin API round-trip.
- **Realm export** committed as `manifests/keycloak/realm-cvops.json` for repeatable, greenfield bootstrap (seed users + a demo org group).

---

## Infrastructure

- **docker-compose** (`manifests/docker-compose.yml`): add `keycloak` service behind a new profile (`auth`, on by default once stable; gate like CVAT/training while iterating). Use a non-root Keycloak image (per the non-root-containers rule — verify before choosing the tag). Back it with its own Postgres database/schema (separate DB on the existing postgres, or a dedicated `keycloak` db). Mount the realm import.
- **Tilt** (`Tiltfile`): bring Keycloak up as infra (container) for `tilt up`, with realm import on first boot; expose on a stable local port; API/worker get `KEYCLOAK_URL` env.
- **nginx** edge: no change needed if KC is reached directly; optionally proxy `/auth/` → Keycloak for same-origin.

## Config (`services/api/src/cvops_api/config.py`)

Add (and remove the HS256-specific ones once cut over):
- `KEYCLOAK_URL`, `KEYCLOAK_REALM=cvops`
- `KEYCLOAK_API_AUDIENCE=cvops-api`, `KEYCLOAK_ISSUER` (derived)
- `KEYCLOAK_JWKS_URL` (derived), JWKS cache TTL
- `OIDC_CLIENT_ID=cvops-frontend` (frontend build env: `VITE_KEYCLOAK_URL`, `VITE_KEYCLOAK_REALM`, `VITE_KEYCLOAK_CLIENT_ID`)
- Keep `WORKER_TOKEN` OR add `cvops-worker` client creds — decide with #30.
- Deprecate/remove: `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`.

---

## Frontend changes

- Add `keycloak-js` (or `oidc-client-ts`); init on app load, silent SSO check.
- `lib/client.ts`: replace localStorage token plumbing + manual refresh with the
  KC adapter's `token` + `updateToken()`; attach `Authorization` from KC.
- `api/auth.ts`: `login()` → `keycloak.login()`; `logout()` → `keycloak.logout()`;
  `register()` → KC registration URL; `useMe()` reads from `/auth/me` (still served
  by API from the mirror) or from KC userinfo.
- `pages/Login.tsx`: becomes a redirect/guard, not a credential form (resolves #68's login/register concern by removing the form).
- Route guard: redirect unauthenticated → KC; handle the redirect callback.

## API endpoint changes (`routers/auth.py`)

- **Remove** `/register`, `/token`, `/refresh`, `/revoke` (KC owns these).
- **Keep** `/me` — serves the mirrored `User` (now resolved via `kc_sub`).
- `get_viewer_user` (`?token=` query param for the `/dataset` viewer): keep the
  query-param entry but validate the token via the new RS256 path.

## RBAC (#72) — `core/rbac.py`

- Implement `require_role(*allowed)` dependency that reads roles from the token
  claim (mirrored to `Membership.role`) and 403s otherwise.
- Apply to mutating routes (write = `owner`/`editor`; read = any member).
- Cover with tests (the existing testcontainers suite).

---

## Testing strategy

The suite uses real Postgres via testcontainers and currently mints HS256 tokens
directly. Two options:
1. **Test signing key** — generate an RSA keypair in test fixtures, serve a fake
   JWKS (monkeypatch the JWKS fetch), mint RS256 tokens with arbitrary claims.
   Fast, no KC container. **Recommended.**
2. KC testcontainer — heavy, slow; reserve for one end-to-end smoke test.

Update `tests/routers/test_auth.py` + the auth helper fixtures to the RS256/claims
model; add RBAC tests (#72) and `/internal` auth tests (#30, #76).

---

## Rollout (phased, each a separate PR into `dev`)

1. **Infra** — Keycloak in compose + Tilt + committed realm export; seed users. No app change yet. *(Chore)*
2. **API resource-server** — RS256/JWKS validation in `core/auth.py`, `User.kc_sub` column + migration, JIT mirroring; keep old `/token` temporarily behind a flag for test transition. *(Feat)*
3. **Cut over endpoints** — remove `/register|/token|/refresh|/revoke`; `/me` from mirror; drop Redis blacklist + HS256 config. *(Refactor)*
4. **Frontend** — keycloak-js adapter, redirect login, client.ts rewrite. *(Feat)*
5. **RBAC (#72)** — implement + enforce + tests. *(Feat)*
6. **Worker / internal (#30)** — service-account or `verify_worker`; typed CVAT webhook schema. *(Fix/Security)*
7. **Members UI (#61)** — invite/role via KC groups (API proxy to KC Admin API, or Account console link). *(Feat)*
8. **Docs** — update `services/api/CLAUDE.md` §3 Auth, root `CLAUDE.md` auth/arch, README API table; this file. *(Docs)*

## Open questions / risks

- **`/internal/*` auth** (#30): KC service-account vs keep `WORKER_TOKEN`. Service-account is cleaner long-term but adds a confidential client + token fetch to the worker. Recommend keeping `WORKER_TOKEN` for the worker doorbell and just adding `verify_worker`; revisit.
- **Admin API surface for members UI** (#61): exposing KC group management through our API needs a confidential admin client — scope carefully (owner-only).
- **Migration column**: app FKs target `users.id` (our UUID). Keep our UUID as PK; store `kc_sub` as a unique secondary key. Do **not** repoint FKs at `kc_sub`.
- **Greenfield assumption**: existing dev users are discarded; communicate the reset.
- Non-root container rule applies to the Keycloak image — verify the chosen tag's default user before adding it.
