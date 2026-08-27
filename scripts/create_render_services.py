"""Provision the two Render web services: the API, and the app that serves
the SPA together with Better Auth at /api/auth/*.

Idempotent: existing services with the target names are reported, not recreated.

Usage:
    python scripts/create_render_services.py --dry-run
    python scripts/create_render_services.py

Secrets are read from .env and sent to Render as service environment variables.
They are never printed. RENDER_API_KEY is deliberately NOT propagated to the
deployed service - see PRD section 7.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

API = "https://api.render.com/v1"
ROOT = Path(__file__).resolve().parent.parent

REPO = "https://github.com/simonraj79/AgenticRAG"
BRANCH = "main"
REGION = "singapore"      # MUST be explicit - the API defaults to "oregon"

BACKEND_NAME = "agentic-rag-api"

# The service that serves the SPA is a WEB SERVICE, not a static site, and the
# name changed with it. `agentic-rag-web` was a static site and was deleted on
# 2026-08-27, because it could not run Node and therefore could not host Better
# Auth at /api/auth/*. Sign-in from it did not error: the static host answered
# the sign-in POST with a bare 200 and an empty body, Better Auth read the 200
# as success, no redirect URL came back, and the button spun for ever.
#
# Serving the SPA from the SAME process that owns /api/auth/* is what makes the
# session cookie first-party -- onrender.com is on the Public Suffix List, so
# two Render subdomains are different SITES and a cookie set by one is
# third-party to the other. A topology, not a preference, and Render offers no
# static-site -> web-service conversion.
FRONTEND_NAME = "agentic-rag-app"

# Secrets forwarded to the backend. RENDER_API_KEY is intentionally absent.
#
# **OPENROUTER_API_KEY was missing from this list until 2026-08-16**, and the way
# it was missing is the lesson. This script predates the move off the Gemini API;
# when chat moved to OpenRouter, `llm.py`, `config.py` and `.env.example` were all
# updated and the PROVISIONING script was not, because nothing re-provisions on a
# working deployment. The live service has the key -- somebody added it by hand --
# so the gap was invisible for as long as nobody created a service.
#
# It would have surfaced as a fresh backend where generation, the rewrite
# decision, golden-set drafting and the Ragas judge all fail at once, on a deploy
# whose build and health check both pass. Every diagnostic would point at the
# model layer.
#
# The general shape, worth checking whenever a provider is added: a required
# environment variable has THREE homes in this repo -- `config.py`,
# `.env.example`, and here -- and only the first two are exercised by running the
# app locally.
BACKEND_SECRET_KEYS = [
    # R2 first, because it is the only group whose absence stops the service
    # BOOTING rather than failing a later call: `storage_route` defaults to "r2"
    # and its validator refuses to construct Settings without these three. They
    # must be set BEFORE the deploy that first ships app/storage.py, not after.
    #
    # R2_API_TOKEN is deliberately absent. It creates and deletes buckets, the
    # service never needs it, and the rule is RENDER_API_KEY's.
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "PINECONE_API_KEY",
    "PINECONE_INDEX_NAME",
    "COHERE_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "SESSION_SECRET_KEY",
]

# NOT a secret, which is why it is absent from the list above -- and it still
# belongs on the service, where it is currently not set at all. `EMBEDDING_ROUTE`
# picks which gateway embeds ("openrouter" ships, "google" is the rollback), and
# the deployed backend is on the correct road today only because `config.py`'s
# default happens to be the shipped one. That is the drift shape the paragraph
# above warns about, with the sign reversed: not a variable missing from the
# template, but a variable whose correctness is INHERITED rather than declared --
# so the rollback becomes a code change instead of a dashboard edit, and a future
# change of default moves production silently. It is the one setting whose fault
# returns confident nonsense rather than an error. Set it explicitly on the
# service; `BACKEND_LITERAL_ENV` below is where it would go.

# Sent as a literal, not read from `.env`, and it must not be omitted.
#
# `settings.environment` DEFAULTS to "development", which is one of the three
# gates on `POST /api/auth/dev-login` -- an authentication bypass. The other two
# (a loopback client address, and `DEV_AUTH_ENABLED`) would still hold on Render,
# but leaving the value at its development default means a deployed service is one
# environment-variable edit away from the bypass being reachable rather than two.
BACKEND_LITERAL_ENV = {
    "ENVIRONMENT": "production",
    # Not a secret -- a bucket name is not a credential -- so it is sent as a
    # literal rather than copied from `.env`, the same treatment ENVIRONMENT
    # gets. It is here rather than omitted because `config.py`'s default and
    # this line disagreeing would be a silent split-brain: the service would
    # read and write a different bucket from every local script.
    "R2_BUCKET": "groundwork-media",
}


def call(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {method} {path}\n{e.read().decode(errors='replace')[:600]}")
        raise


def find_service(token: str, name: str) -> dict | None:
    for row in call("GET", "/services?limit=100", token):
        svc = row.get("service", row)
        if svc.get("name") == name:
            return svc
    return None


def set_env_var(token: str, service_id: str, key: str, value: str) -> None:
    """Update one env var without disturbing the others.

    PUT /env-vars (no key) REPLACES the whole set - avoid it.
    """
    call("PUT", f"/services/{service_id}/env-vars/{key}", token, {"value": value})


def wire(token: str) -> int:
    """Set the cross-references that can only be known after creation.

    Render appends a random suffix to service hostnames, so neither URL can be
    predicted before the services exist.
    """
    backend = find_service(token, BACKEND_NAME)
    frontend = find_service(token, FRONTEND_NAME)
    if not backend or not frontend:
        print("ERROR: both services must exist before wiring.")
        return 1

    backend_url = backend["serviceDetails"].get("url", "")
    frontend_url = frontend["serviceDetails"].get("url", "")
    redirect_uri = f"{backend_url}/api/auth/google/callback"
    better_auth_redirect = f"{frontend_url}/api/auth/callback/google"

    print(f"backend  {backend_url}")
    print(f"frontend {frontend_url}\n")

    # BETTER_AUTH_URL is the origin the app service answers on, which is ALSO
    # the origin serving the SPA -- they are the same string on purpose, and
    # `frontend_url` is that string. Both services read it and neither can be
    # left out:
    #
    #   the API   derives the JWKS URL, the expected `iss` and a CORS origin
    #             from it. Unset, every signed-in user gets a 401 and the
    #             traceback names JSON parsing, not configuration.
    #   the app   uses it as Better Auth's own baseURL and trustedOrigin.
    #             Unset, the service refuses to boot -- which is the good case.
    #
    # It was missing here until 2026-08-27, in exactly the way this file already
    # warns about above: a required variable has three homes and only the two
    # exercised by running the app locally were kept in step.
    set_env_var(token, backend["id"], "BETTER_AUTH_URL", frontend_url)
    set_env_var(token, frontend["id"], "BETTER_AUTH_URL", frontend_url)

    set_env_var(token, backend["id"], "FRONTEND_URL", frontend_url)
    set_env_var(token, backend["id"], "OAUTH_REDIRECT_URI", redirect_uri)
    set_env_var(token, frontend["id"], "VITE_API_URL", backend_url)
    print("Set BETTER_AUTH_URL, FRONTEND_URL and OAUTH_REDIRECT_URI on the "
          "backend, BETTER_AUTH_URL and VITE_API_URL on the app service.")

    # An env-var PUT returns 200 and does NOT restart anything. The process
    # keeps the environment it booted with, so the API can report a value the
    # running worker has never seen -- measured on 2026-08-27, when every
    # request from the new frontend failed CORS while the env-var listing read
    # correctly. Presence in the API is not presence in the PROCESS.
    print("")
    print("Now trigger a deploy of BOTH services, or none of the above reaches")
    print("the running processes:")
    print("")
    print("  curl -s -X POST -d '{}'" )
    print("       -H 'Content-Type: application/json'")
    print("       -H 'Authorization: Bearer <RENDER_API_KEY>'")
    print("       https://api.render.com/v1/services/<id>/deploys")

    print("")
    print("*** MANUAL STEP ***")
    print("Add BOTH of these EXACT strings to the Google OAuth client's")
    print("authorized redirect URIs (console.cloud.google.com/auth/clients).")
    print("Google exposes no API for this:")
    print("")
    print(f"    {better_auth_redirect}   <- Better Auth, the live path")
    print(f"    {redirect_uri}   <- Authlib, the legacy path")
    print("")
    print("The shapes differ and that is not a typo: Better Auth's is")
    print("/api/auth/callback/{provider} on the APP service, Authlib's is")
    print("/api/auth/google/callback on the API service. Both entries coexist")
    print("until Authlib is removed.")
    print("Matching is exact - scheme, case and trailing slash all count.")
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    load_dotenv(ROOT / ".env")

    if "--wire" in sys.argv:
        token = os.getenv("RENDER_API_KEY")
        if not token:
            print("ERROR: RENDER_API_KEY not set in .env")
            return 1
        return wire(token)

    token = os.getenv("RENDER_API_KEY")
    if not token:
        print("ERROR: RENDER_API_KEY not set in .env")
        return 1

    internal_db = os.getenv("DATABASE_URL_INTERNAL", "")
    if not internal_db:
        print("ERROR: DATABASE_URL_INTERNAL not set. Run scripts/create_render_db.py first.")
        return 1

    owner_id = call("GET", "/owners?limit=20", token)[0]["owner"]["id"]
    print(f"Workspace: {owner_id}\n")

    missing = [k for k in BACKEND_SECRET_KEYS if not os.getenv(k)]
    if missing:
        print(f"WARNING: these are empty in .env and will be sent blank: {missing}\n")

    # ---------------- Backend ----------------
    backend = find_service(token, BACKEND_NAME)
    if backend:
        print(f"Web service '{BACKEND_NAME}' already exists ({backend['id']}).")
    else:
        env_vars = [{"key": k, "value": os.getenv(k, "")} for k in BACKEND_SECRET_KEYS]
        # The service reaches Postgres over the private network, so it gets the
        # INTERNAL url. Same region is what makes that resolve at all.
        env_vars.append({"key": "DATABASE_URL", "value": internal_db})
        env_vars.append({"key": "PYTHON_VERSION", "value": "3.12.10"})
        env_vars += [{"key": k, "value": v} for k, v in BACKEND_LITERAL_ENV.items()]

        payload = {
            "type": "web_service",
            "name": BACKEND_NAME,
            "ownerId": owner_id,
            "repo": REPO,
            "branch": BRANCH,
            "rootDir": "backend",
            "autoDeploy": "yes",
            "envVars": env_vars,
            "serviceDetails": {
                "runtime": "python",
                "plan": "starter",          # lowest paid tier
                "region": REGION,
                "healthCheckPath": "/api/health",
                "envSpecificDetails": {
                    "buildCommand": "pip install -r requirements.txt",
                    # Migrations run at START, not build: the internal database
                    # host does not resolve from Render's build environment.
                    "startCommand": (
                        "alembic upgrade head && "
                        "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
                    ),
                },
            },
        }
        print(f"Creating web service '{BACKEND_NAME}' "
              f"(python, starter, {REGION}, rootDir=backend)")
        if dry_run:
            print("  --dry-run; not created")
        else:
            created = call("POST", "/services", token, payload)
            backend = created.get("service", created)
            print(f"  created: {backend['id']}")

    # ---------------- Frontend ----------------
    backend_url = (backend or {}).get("serviceDetails", {}).get("url") or ""
    api_url = backend_url or f"https://{BACKEND_NAME}.onrender.com"

    frontend = find_service(token, FRONTEND_NAME)
    if frontend:
        print(f"\nApp service '{FRONTEND_NAME}' already exists ({frontend['id']}).")
    else:
        # REFUSED, DELIBERATELY, AND NOT BECAUSE IT IS HARD TO AUTOMATE.
        #
        # What used to be here created a STATIC SITE. That is now a broken
        # deployment rather than a lesser one: a static site cannot run Node, so
        # it cannot serve /api/auth/*, and the SPA it publishes looks for Better
        # Auth on its own origin. The failure is SILENT -- Render's static host
        # answers the sign-in POST with a bare 200 and an empty body, Better
        # Auth reads the 200 as success, no redirect URL comes back, and the
        # sign-in button spins for ever with no error anywhere to find. That
        # shipped once and cost a browser session to diagnose.
        #
        # The replacement is a web service whose build produces BOTH halves and
        # whose start command runs the Node process, and it holds secrets a
        # static site never did -- BETTER_AUTH_SECRET, the Google client secret,
        # DATABASE_URL. So it is not a two-line edit to the old payload, and
        # provisioning it blind is not attempted. Refusing beats guessing.
        print("")
        print(f"ERROR: no service named '{FRONTEND_NAME}' exists, and this script")
        print("will NOT create one. It must be a WEB SERVICE serving the SPA and")
        print("Better Auth from ONE origin -- a static site cannot, and the")
        print("resulting sign-in failure is silent. Create it in the dashboard:")
        print("")
        print("  type     web_service (Node), rootDir = repo root")
        print("  build    cd frontend && npm ci --include=dev && npm run build &&")
        print("           cd ../auth && npm ci --include=dev && npm run build &&")
        print("           rm -rf public && cp -r ../frontend/dist public")
        print("  start    cd auth && node dist/index.js")
        print("  env      NODE_ENV=production, BETTER_AUTH_URL, BETTER_AUTH_SECRET,")
        print("           DATABASE_URL, GOOGLE_OAUTH_CLIENT_ID, ")
        print(f"           GOOGLE_OAUTH_CLIENT_SECRET, VITE_API_URL={api_url}")
        print("")
        print("`--include=dev` is not optional: NODE_ENV=production makes npm omit")
        print("devDependencies, and `tsc -b` then cannot find its types.")
        print("")
        print("Then re-run with --wire. See auth/.env.example.")
        return 1

    if dry_run:
        return 0

    print("\n--- Result ---")
    for label, svc in (("backend", backend), ("frontend", frontend)):
        if not svc:
            continue
        details = svc.get("serviceDetails", {})
        print(f"{label:9} {svc.get('name'):18} {svc.get('id'):32} "
              f"{details.get('url') or '(url pending first deploy)'}")

    print("\nNext: run --wire to set FRONTEND_URL and OAUTH_REDIRECT_URI once "
          "Render has assigned the real hostnames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
