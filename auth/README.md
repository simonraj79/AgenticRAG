# `auth/` — Better Auth identity service

Google sign-in, on Node, in front of the SPA. Issues a JWT that FastAPI verifies
offline. This service owns identity and nothing else.

## Why it exists in this shape

FastAPI cannot run Better Auth — it is a TypeScript library. So identity crosses
the language boundary as a **signed assertion** rather than a session lookup:

```
browser ──► auth service  (Hono)      /api/auth/*   Better Auth + Google
        │                             /*            the built SPA
        │                             cookie is FIRST-PARTY here
        │
        └──► API  (FastAPI)           Authorization: Bearer <jwt>
                                      verified against /api/auth/jwks
```

**The co-location is the feature, not a convenience.** `onrender.com` is on the
Public Suffix List, so the SPA host and the API host are different *sites*. A
session cookie sent to the API is third-party and is dropped by Safari,
Incognito, Brave and strict-mode Firefox — the "signs in, lands back on the
login page" bug. Serving the SPA from this process makes the cookie first-party;
a header carries the identity to the API, and a header has no site.

Putting this service on its own Render subdomain **reproduces the bug**.

## Local setup

```bash
# 1. Secret
openssl rand -base64 32

# 2. cp .env.example .env   and fill DATABASE_URL, BETTER_AUTH_SECRET,
#    GOOGLE_OAUTH_CLIENT_ID/SECRET (the same pair the backend .env holds)

# 3. Google console -> Clients -> your Web client -> Authorized redirect URIs
#    ADD (do not replace):
#      http://localhost:5173/api/auth/callback/google
#    Console only; Google exposes no API for this. Matching is EXACT.

# 4. Schema. Two migrations, two systems, in this order.
cd ../backend && python -m alembic upgrade head    # users.better_auth_id
cd ../auth    && npm install && npm run migrate    # user/session/account/verification/jwks

# 5. Three processes
cd backend && uvicorn app.main:app --reload --port 8000
cd auth    && npm run dev            # :3000
cd frontend && npm run dev           # :5173, proxies /api/auth -> :3000
```

Open <http://localhost:5173>. **Sign in with Google** is Better Auth; *Having
trouble? Use the previous sign-in* is the Authlib path, still live.

## Verifying it actually worked

A green harness is not evidence here — `scripts/auth_check.py` proves the
verifier is correct, never that a login happened. Read these by eye:

| Check | Expected |
|---|---|
| `curl localhost:5173/api/auth/jwks` | a JSON key set, **not** the SPA's HTML |
| Sign in, then DevTools ▸ Network ▸ any `/api/...` call | an `Authorization: Bearer` header |
| `SELECT email, better_auth_id FROM users WHERE better_auth_id IS NOT NULL` | **your existing row**, now linked — not a new one |
| Your agents and documents after sign-in | all still there |

That third row is the one that matters. If a *new* `users` row appears instead
of an existing one being linked, `app/auth/identity.py` did not find the Google
subject and every prior workspace is invisible. Stop and read that module.

## Deploying

The frontend Render service changes from a **Static Site** to a **Web Service**,
because it now runs a Node process.

```
Build   cd frontend && npm ci && npm run build \
        && cd ../auth && npm ci && npm run build \
        && rm -rf public && cp -r ../frontend/dist public
Start   node dist/index.js
```

Env vars on that service: `DATABASE_URL` (**internal** host), `BETTER_AUTH_URL`
(the service's own https URL), `BETTER_AUTH_SECRET`, `GOOGLE_OAUTH_CLIENT_ID`,
`GOOGLE_OAUTH_CLIENT_SECRET`, `NODE_ENV=production`.

On the **API** service add `BETTER_AUTH_URL` pointing at the web service, so
FastAPI knows where the JWKS lives and what `iss` to expect.

Add the production redirect URI to the Google console:
`https://<web-host>/api/auth/callback/google`.

> Render env vars **drift** from `.env` and presence is not correctness — this
> project already shipped a production Cohere key that was silently the trial
> one. Compare by value via the API, not by eye.

## Known gap, before Authlib is removed

`api.exportUrl` and `api.downloadUrl` in `frontend/src/lib/api.ts` build URLs
handed to the browser as `<a href>` and `<img src>`. **A navigation cannot carry
an `Authorization` header**, so those two are authenticated by the Authlib
cookie and by nothing else.

Deleting the cookie path breaks them — for precisely the users this cutover was
built for. Fix first: fetch through `api()` and hand back a blob URL, or accept
a short-lived token as a query parameter. Both call sites carry a comment saying
so.

## Files

| Path | What |
|---|---|
| `src/auth.ts` | Better Auth config: pg pool, Google, `jwt()`, TLS-shape detection |
| `src/index.ts` | Hono host: `/api/auth/*` then static SPA, binds `0.0.0.0` |
| `../backend/app/auth/jwt.py` | JWKS cache + JWT verification (the trust decision) |
| `../backend/app/auth/identity.py` | Better Auth user → this app's `users` row |
| `../backend/app/db/migration_filter.py` | stops `--autogenerate` dropping these tables |
| `../scripts/auth_check.py` | 31 offline cases, incl. `alg:none` and HS256 confusion |
