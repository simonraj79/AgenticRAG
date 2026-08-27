"""Layer 1 harness for the Better Auth cutover. No DB, no network, no model.

    backend/.venv/Scripts/python.exe scripts/auth_check.py

WHY THIS FILE EXISTS.

Moving identity to Better Auth introduces a Node service that FastAPI cannot
call and must nevertheless trust. Three things about that arrangement fail
SILENTLY, which is the only reason this harness is worth its weight:

1. ALEMBIC WILL DROP THE AUTH SYSTEM. Better Auth owns five tables that have no
   SQLAlchemy model -- `user`, `session`, `account`, `verification`, `jwks`. The
   app's own tables are `users` and `sessions`, PLURAL, so there is no name
   collision and nothing complains. `--autogenerate` therefore sees five unknown
   tables and emits DROP TABLE for each. The migration reviews cleanly. It fires
   on the next UNRELATED schema change, so whoever hits it will not connect it
   to this work. Cases 1-6.

2. JWT VERIFICATION CAN PASS WHILE PROVING NOTHING. A verifier that forgets to
   pin `algorithms=` accepts `alg: none`, and one that accepts HMAC alongside
   EdDSA accepts a token signed with the PUBLIC key as the shared secret. Both
   return a well-formed claims dict. Cases 15 and 16 are those two attacks,
   because a verifier that only ever sees valid tokens passes every happy-path
   test ever written.

3. A FALLBACK THAT NEVER YIELDS IS INDISTINGUISHABLE FROM ONE THAT WORKS. The
   cutover keeps the old cookie path live beside the new Bearer path. If Bearer
   silently loses to the cookie, every request still succeeds -- on the OLD
   system -- and the new one looks deployed when it is not. Cases 19-24.

The pattern this repository keeps re-learning: trigger on the absence of the
outcome you wanted, never on the presence of an error. Nothing above raises.

ASCII in print(), exit 1 on any failure -- `new features/06-test-plan.md`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "[ok]  " if condition else "[FAIL]"
    print(f"{status} {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def read(rel: str) -> str:
    p = REPO / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------------
# 1-6. The alembic filter. Tested by CALLING it, not by grepping for it -- and
#      then separately by reading env.py, because a filter that exists and is
#      not wired in is the exact failure this is guarding against.
# ---------------------------------------------------------------------------
print("-- alembic: Better Auth tables must survive --autogenerate --")

try:
    from app.db.migration_filter import BETTER_AUTH_TABLES, include_object

    imported = True
except Exception as exc:  # noqa: BLE001
    imported = False
    print(f"       import failed: {exc}")

check("1. app/db/migration_filter.py is importable", imported)

if imported:
    check(
        "2. every Better Auth table is excluded from autogenerate",
        all(
            include_object(None, t, "table", True, None) is False
            for t in BETTER_AUTH_TABLES
        ),
        f"tables={sorted(BETTER_AUTH_TABLES)}",
    )
    check(
        "3. the app's OWN tables are still compared",
        all(
            include_object(None, t, "table", True, None) is True
            for t in ("users", "sessions", "agents", "documents", "queries")
        ),
        "a filter wide enough to hide 'user' must not hide 'users'",
    )
    check(
        "4. non-table objects (index, column) are never filtered",
        include_object(None, "ix_sessions_user_expires", "index", True, None) is True
        and include_object(None, "email", "column", True, None) is True,
    )

env = read("backend/alembic/env.py")
check(
    "5. env.py imports the filter",
    "from app.db.migration_filter import include_object" in env,
)
# Coverage, not behaviour: there are two configure() calls, and wiring only one
# of them leaves offline mode dropping the tables. This asserts the CALL SITES,
# which is the half a behavioural test structurally cannot reach.
_wired = len(re.findall(r"include_object=include_object", env))
check(
    "6. BOTH context.configure calls pass include_object",
    _wired == 2,
    f"found {_wired} of 2",
)


# ---------------------------------------------------------------------------
# 7-18. JWT verification, against a keypair generated right here. No network:
#       the point is that verification is a pure function of (token, jwks).
# ---------------------------------------------------------------------------
print("\n-- JWT verification (offline, real Ed25519 keypair) --")

try:
    from app.auth.jwt import InvalidToken, extract_bearer, verify_token

    jwt_imported = True
except Exception as exc:  # noqa: BLE001
    jwt_imported = False
    print(f"       import failed: {exc}")

check("7. app/auth/jwt.py is importable", jwt_imported)

if jwt_imported:
    import base64
    import json
    import time

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    ISS = "https://auth.example.com"
    AUD = "https://api.example.com"
    KID = "test-key-1"

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    raw_pub = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    JWKS = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": KID, "x": b64u(raw_pub)}]}

    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    def mint(**overrides) -> str:
        now = int(time.time())
        claims = {
            "sub": "google-sub-123",
            "email": "a@b.com",
            "iss": ISS,
            "aud": AUD,
            "iat": now,
            "exp": now + 900,
        }
        claims.update(overrides)
        return pyjwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": KID})

    def rejects(token: str) -> bool:
        try:
            verify_token(token, JWKS, issuer=ISS, audience=AUD)
            return False
        except InvalidToken:
            return True

    good = mint()
    try:
        claims = verify_token(good, JWKS, issuer=ISS, audience=AUD)
    except Exception as exc:  # noqa: BLE001
        claims = {}
        print(f"       verify_token raised on a valid token: {exc}")

    check("8. a valid token verifies", claims.get("sub") == "google-sub-123")
    check("9. claims survive verification", claims.get("email") == "a@b.com")
    check("10. an expired token is rejected", rejects(mint(exp=int(time.time()) - 60)))
    check("11. a wrong issuer is rejected", rejects(mint(iss="https://evil.example")))
    check("12. a wrong audience is rejected", rejects(mint(aud="https://evil.example")))
    # THE CASE THAT WAS MISSING, AND THE BUG IT LET THROUGH.
    #
    # Every case above passes `audience=AUD` against a token that carries `aud`,
    # so the pairing was never varied. Better Auth SETS `aud` (to its baseURL),
    # while `BETTER_AUTH_AUDIENCE` is empty by default -- so production ran the
    # one combination no case covered, and pyjwt does NOT treat `audience=None`
    # as "skip": `_validate_aud` raises InvalidAudienceError when the token has
    # an `aud` the caller did not ask for. Every signed-in user got a 401 with a
    # verifier that was, by every existing assertion, correct.
    #
    # This asserts the SEMANTICS this module documents -- "empty means do not
    # verify aud" -- rather than the behaviour pyjwt happens to have.
    _no_aud_configured = None
    try:
        _no_aud_configured = verify_token(mint(), JWKS, issuer=ISS, audience=None)
    except InvalidToken as exc:
        print(f"       audience=None rejected a token carrying aud: {exc}")
    check(
        "12b. audience=None ACCEPTS a token that carries aud",
        (_no_aud_configured or {}).get("sub") == "google-sub-123",
        "pyjwt reads audience=None as 'assert no aud', not as 'do not check'",
    )
    check(
        "12c. a configured audience is still enforced",
        rejects(mint(aud="https://evil.example")),
        "the fix must not disable the check when one IS configured",
    )

    check("13. a tampered payload is rejected", rejects(good[:-4] + "AAAA"))
    check("14. garbage is rejected, not raised as ValueError", rejects("not.a.token"))

    # -- the two a happy-path suite always passes and never tests -------------
    _h = json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode()
    _b = json.dumps(
        {"sub": "attacker", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 900}
    ).encode()
    check(
        "15. alg:none is rejected (algorithms= must be pinned)",
        rejects(f"{b64u(_h)}.{b64u(_b)}."),
        "an unpinned verifier accepts this and returns clean claims",
    )

    # The classic confusion: sign HS256 using the PUBLIC key bytes as the HMAC
    # secret. A verifier allowing both families treats a public value as shared.
    confused = pyjwt.encode(
        {"sub": "attacker", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 900},
        raw_pub,
        algorithm="HS256",
        headers={"kid": KID},
    )
    check(
        "16. an HS256 token signed with the PUBLIC key is rejected",
        rejects(confused),
        "algorithm confusion -- a public key is not a secret",
    )

    check(
        "17. an unknown kid is rejected",
        rejects(
            pyjwt.encode(
                {"sub": "x", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 900},
                pem,
                algorithm="EdDSA",
                headers={"kid": "nope"},
            )
        ),
    )
    check(
        "18. a token with NO kid is rejected",
        rejects(
            pyjwt.encode(
                {"sub": "x", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 900},
                pem,
                algorithm="EdDSA",
            )
        ),
        "not merely tried against every key in the set",
    )


# ---------------------------------------------------------------------------
# 19-24. The Authorization header, and the cutover precedence.
# ---------------------------------------------------------------------------
print("\n-- bearer parsing and cutover precedence --")

if jwt_imported:
    check(
        "19. a well-formed header yields the token",
        extract_bearer("Bearer abc.def.ghi") == "abc.def.ghi",
    )
    check(
        "20. the scheme is case-insensitive (RFC 7235)",
        extract_bearer("bearer abc") == "abc",
    )
    check("21. a missing header yields None", extract_bearer(None) is None)
    check(
        "22. a non-Bearer scheme yields None",
        extract_bearer("Basic dXNlcjpwdw==") is None
        and extract_bearer("Bearer") is None,
    )

deps = read("backend/app/auth/deps.py")
check(
    "23. deps.py reads the Authorization header",
    "extract_bearer" in deps,
    "the Bearer path must exist in the dependency, not only in jwt.py",
)
# The silent failure named in the docstring: if the cookie is consulted first
# and wins, every request still succeeds on the OLD system.
_cookie_read = "request.cookies.get(SESSION_COOKIE)"
check(
    "24. Bearer is attempted BEFORE the session cookie",
    (
        "extract_bearer" in deps
        and _cookie_read in deps
        and deps.index("extract_bearer") < deps.index(_cookie_read)
    ),
    "cookie-first makes a dead Bearer path look deployed",
)


# ---------------------------------------------------------------------------
# 25-31. The Node service config, read as source. It cannot be imported from
#        Python, so these assert the settings whose absence is silent.
# ---------------------------------------------------------------------------
print("\n-- auth service configuration --")

auth_ts = read("auth/src/auth.ts")
index_ts = read("auth/src/index.ts")
pkg = read("auth/package.json")

check("25. auth/src/auth.ts exists", bool(auth_ts))
check(
    "26. the jwt plugin is enabled",
    "jwt(" in auth_ts,
    "FastAPI has no other way to trust a login",
)
check("27. Google is the configured social provider", "google" in auth_ts)
check(
    "28. trustedOrigins is set",
    "trustedOrigins" in auth_ts,
    "absent, Better Auth answers Invalid Origin",
)
check("29. secure cookies are forced outside development", "useSecureCookies" in auth_ts)
check(
    "30. the SPA is served from the SAME origin as /api/auth",
    bool(index_ts)
    and "/api/auth" in index_ts
    and ("serveStatic" in index_ts or "static" in index_ts),
    "co-location is the entire reason the cookie is first-party",
)
check("31. better-auth is a dependency", '"better-auth"' in pkg)


print("\n" + "=" * 74)
if failures:
    print(f"{len(failures)} FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
