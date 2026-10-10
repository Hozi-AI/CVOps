> **Archived 2026-10-10.** Superseded by [15 · Keycloak user management](../15-keycloak-user-management.md). Kept for history; do not treat as current.

# CVOps — Authentication & User Management Design

> **Decision:** Replace the hand-rolled JWT auth with **Keycloak** (self-hosted OIDC identity provider). FastAPI stops *issuing* tokens and instead *validates* tokens Keycloak issues. Keycloak federates with **Active Directory / LDAP**. CVOps keeps owning orgs/roles/tenancy.
>
> **Constraints that drove this:** on-prem / air-gapped deployment · must federate with existing AD/LDAP · design now, build soon.

---

## 1. Why Keycloak (and not the alternatives)

| Need | Keycloak | Zitadel | Authentik | Cloud SaaS (Clerk/Auth0/WorkOS) |
|---|---|---|---|---|
| Runs fully **air-gapped** | ✅ | ✅ | ✅ | ❌ out (needs internet) |
| **AD / LDAP / Kerberos** federation | ✅ first-class | partial | ✅ | varies |
| **PKI / smartcard (CAC/PIV)** path later | ✅ X.509 built-in | ❌ | partial | ❌ |
| Multi-tenant (realms / orgs) | ✅ realms | ✅ orgs | ✅ | ✅ |
| Asymmetric signing + key rotation (JWKS) | ✅ RS256 | ✅ | ✅ | ✅ |
| Helm chart, fits our k8s/Tilt stack | ✅ | ✅ | ✅ | n/a |
| Battle-tested in gov/defense | ✅ (the default) | newer | newer | n/a |

The AD/LDAP requirement + air-gap + a likely future smartcard requirement make this not really a contest. Keycloak is the defensible choice.

---

## 2. What changes vs. what we keep

### Keep
- The `users`, `orgs`, `memberships` tables — they stay the **authorization** source of truth.
- `get_current_user` as the single FastAPI dependency every router already uses (its *internals* change; its signature/return type does not — so **no router changes**).
- Org/role/tenancy logic — all of it stays in CVOps.

### Replace / remove
- `create_access_token`, `create_refresh_token`, password login (`/auth/token`), registration (`/auth/register`), refresh (`/auth/refresh`) — Keycloak does all of this now.
- `passlib` / bcrypt password hashing — no local passwords once AD federation is on (keep one optional break-glass admin if desired).
- The single shared `JWT_SECRET` (HS256) — replaced by validating against Keycloak's rotating public keys (RS256/JWKS).

### Add
- A `subject` (Keycloak `sub`, a stable UUID) column on `users` — the durable identity key (email can change, `sub` cannot).
- JWKS fetch + cache, and token validation (`iss` / `aud` / `exp` / signature).
- Just-in-time (JIT) user provisioning on first login.

> Note: `User.password_hash` is **already nullable** in the schema — the data model was built anticipating exactly this. Low-friction migration.

---

## 3. Architecture

```
┌─────────────────────┐     1. Login: Auth Code + PKCE      ┌──────────────────────┐
│  SPA (Vite/React)   │ ──────────────────────────────────► │   Keycloak           │
│  oidc-client-ts     │ ◄──────────────────────────────────  │   realm: cvops       │
│                     │     id_token + access_token (RS256)  │   client: cvops-spa  │
└─────────┬───────────┘                                       └──────────┬───────────┘
          │ 2. API calls: Authorization: Bearer <access_token>           │ User Federation
          ▼                                                              ▼
┌─────────────────────┐                                       ┌──────────────────────┐
│   FastAPI           │   validate signature vs JWKS (cached) │  Active Directory /   │
│   core/auth.py      │   check iss / aud / exp               │  LDAP                 │
│                     │   map `sub` → local User (JIT)        └──────────────────────┘
└─────────┬───────────┘
          ▼
┌─────────────────────┐
│  Postgres           │  users (shadow, keyed by `sub`), orgs, memberships
└─────────────────────┘
```

**Split of responsibilities — the key principle:**
- **Keycloak answers "who are you"** (authentication): credentials, AD federation, MFA, smartcard, sessions, password reset.
- **CVOps answers "what can you touch"** (authorization): orgs, roles, multi-tenancy, per-resource `org_id` filtering. Unchanged.

---

## 4. Backend changes (concrete)

### 4.1 `core/auth.py` — from minting to validating
```python
# Before: jwt.encode(...) with a shared HS256 secret
# After:  validate the IdP's RS256 token against its JWKS

from jwt import PyJWKClient            # add pyjwt[crypto]; or keep python-jose + manual JWKS
import jwt

_jwks = PyJWKClient(settings.OIDC_JWKS_URL)   # cached, refetches on new `kid`

def decode_token(token: str) -> dict[str, Any]:
    signing_key = _jwks.get_signing_key_from_jwt(token).key
    return jwt.decode(
        token, signing_key,
        algorithms=["RS256"],
        audience=settings.OIDC_AUDIENCE,        # the SPA/api client id
        issuer=settings.OIDC_ISSUER,            # https://<kc>/realms/cvops
    )
```

`get_current_user` keeps its signature, but its body becomes:
1. `decode_token(token)` → claims.
2. Extract `sub`, `email`, `preferred_username`, and (optionally) `groups` / `realm_access.roles`.
3. **JIT upsert**: find `User` by `subject == sub`; if missing, create it (shadow record).
4. Resolve org/role from CVOps DB (see §5).
5. Return the `User`. Routers are untouched.

### 4.2 `routers/auth.py`
- **Remove:** `/auth/register`, `/auth/token`, `/auth/refresh`, `/auth/revoke`.
- **Keep:** `GET /auth/me` (reads `get_current_user`).
- **Add (optional):** `GET /auth/config` → `{ issuer, client_id, authority }` so the SPA can bootstrap OIDC without hardcoding (or just put these in frontend env).
- **Logout:** SPA clears its tokens + redirects to Keycloak's `end-session` endpoint. No server blacklist needed (see §4.4).

### 4.3 DB migration (new Alembic revision)
- Add `users.subject: str | null, unique, indexed` (the Keycloak `sub`).
- Backfill is N/A for a fresh deploy; for any existing users, link by email on first login.
- `password_hash` stays nullable (now effectively legacy/break-glass only).

### 4.4 Revocation — can we drop the Redis `jti` blacklist?
Mostly yes. With short-lived access tokens (5–15 min) and Keycloak-managed sessions:
- **Logout / forced revocation** is handled by Keycloak (session kill + optional back-channel logout to the API).
- Keep a *small* Redis denylist only if you need *instant* emergency revocation of a specific token before it expires. Otherwise delete it — one less thing to run.

### 4.5 Config additions
```
OIDC_ISSUER     = https://keycloak.internal/realms/cvops
OIDC_JWKS_URL   = https://keycloak.internal/realms/cvops/protocol/openid-connect/certs
OIDC_AUDIENCE   = cvops-spa
AUTH_MODE       = oidc | local     # feature flag, see §7
```

---

## 5. Multi-tenancy: how a user gets an org + role

Two strategies — we default to (B), allow (A) by config:

**(A) Map AD groups → CVOps org/role** at JIT. If AD already models units as groups, read the `groups` claim and map `CN=ProjectX-Admins` → `(org=ProjectX, role=admin)`. Zero manual admin work, but couples CVOps to AD's group naming.

**(B) CVOps-managed (default).** Keycloak only authenticates. On first login the user lands in a **pending** state with no org; a CVOps admin assigns org + role in the Settings UI (the members table already planned in the frontend). Full control, no coupling, domain logic stays in CVOps.

> Recommendation: ship (B) first (simple, decoupled), add (A) as an optional `GROUP_ORG_MAP` config when AD's group structure is known.

---

## 6. Frontend (SPA) integration

- Library: **`oidc-client-ts` + `react-oidc-context`** (modern, maintained, handles Auth Code + PKCE, silent token renew, storage).
- No password/login UI in the app — Keycloak hosts the login page (we can theme it to match the brand later).
- Flow: `<AuthProvider>` → unauthenticated user is redirected to Keycloak → callback route exchanges code → access token kept in memory, silently renewed → axios request interceptor attaches `Authorization: Bearer`. On 401, trigger silent renew, then re-login if that fails.
- This replaces the planned hand-built login/register pages in the frontend design plan.

---

## 7. Build plan (since "design now, build soon")

A feature flag (`AUTH_MODE`) lets us land this incrementally without breaking the 146-test suite.

1. **Stand up Keycloak in dev.** Add a `keycloak` service to `manifests/` (compose, behind a profile) and to Tilt. Realm `cvops`, public PKCE client `cvops-spa`, a few **local** test users (AD comes later).
2. **Backend validation path** behind `AUTH_MODE=oidc`. Implement JWKS validation + JIT provisioning. `AUTH_MODE=local` keeps the old path alive so CI stays green during transition.
3. **DB migration:** add `users.subject`.
4. **Frontend OIDC login** via `react-oidc-context`.
5. **AD/LDAP federation** configured in the Keycloak realm — this is a deploy-time/env step, done once the directory details are known.
6. **Flip default to `oidc`.** Remove dead password/token-minting code.
7. **Smartcard/CAC** (future): enable Keycloak X.509 authenticator — no app changes needed.

### Testing note
The test suite currently mints tokens directly. For OIDC mode, don't spin up Keycloak in unit tests — instead, in tests generate a throwaway **RSA keypair**, serve a fake JWKS, and sign test tokens with it (the app validates against that test key). Fast, hermetic, no extra container. Keep one optional integration test that runs a Keycloak testcontainer for the real end-to-end path.

---

## 8. Open questions to resolve before/while building

1. **AD/LDAP specifics** — directory type (AD vs generic LDAP), bind account, base DN, how units/projects map to groups. Needed for §5(A) and step 5.
2. **Org assignment policy** — strategy (A) vs (B) above. Default (B) unless AD groups already model the orgs cleanly.
3. **Access-token lifetime** — 5 min (tighter, more renews) vs 15 min (current). Recommend 5–10 min with silent renew.
4. **Break-glass admin** — keep one local-password admin for when AD/Keycloak is unreachable? (Recommended yes.)
5. **Smartcard/CAC** — required at launch or later? (Affects Keycloak hardening priority; architecture already supports it.)

---

*Bottom line: you already had 80% of the right data model (nullable password, org/membership split). This isn't a rewrite — it's swapping the token *issuer* for Keycloak, changing `core/auth.py` from "encode" to "validate," and adding one column. Everything else — every router, the whole tenancy model — stays exactly as it is.*
