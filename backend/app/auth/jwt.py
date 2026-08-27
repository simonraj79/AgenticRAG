"""Trusting a login that happened in a different process, in a different language.

Better Auth runs on Node, in front of the SPA. FastAPI cannot call it: there is
no Python client, and `auth.api.getSession()` is a TypeScript function holding a
database handle we deliberately do not share. So identity crosses the boundary
as a signed assertion -- a JWT the SPA obtains from Better Auth and presents to
this API -- and this module is the whole of the trust decision.

WHY A JWT AND NOT A SESSION LOOKUP.

The alternative is for FastAPI to forward the browser's cookie to the auth
service's `/api/auth/get-session` on every request. That works and it costs a
network round trip per request, adds the auth service to this API's uptime
dependencies, and puts a second service in the path of `app/api/stream.py`,
which is the one endpoint in this codebase that must not acquire a buffering
intermediary. Signature verification is local, offline after the first JWKS
fetch, and cannot fail because another Render service is cold-starting.

WHAT MAKES THIS FILE DANGEROUS.

Every function here returns a plain dict on success. A verifier that is
comprehensively wrong -- one that forgets to pin `algorithms=` -- also returns a
plain dict, for a token the attacker wrote. There is no error, no log line and
no difference at the call site. Two attacks in particular are invisible to any
test written from valid tokens only:

  alg: none            An unpinned `jwt.decode` accepts an unsigned token whose
                       header says no signature was applied.

  HS256 confusion      The verifier is handed a PUBLIC key. If HMAC is in the
                       allowed set, an attacker signs `HS256` using that public
                       value as the shared secret, and it verifies -- because a
                       public key is public and a shared secret is not.

Both are pinned by `auth_check.py` cases 15 and 16. The defence here is
structural rather than a list of denied names: the allowed algorithm is read off
the KEY, never off the token's own header, and only asymmetric families are
resolvable at all. An attacker controls the header; they do not control the
JWKS.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt as pyjwt

# Asymmetric only, and this set is the security control rather than a
# convenience. HMAC families (HS*) are absent BY CONSTRUCTION, not by denial:
# there is no key in a JWKS this API fetches that could legitimately be an HMAC
# secret, so an `alg: HS256` token can never resolve to a usable algorithm here.
# `none` is absent for the same reason and needs no special case.
_ASYMMETRIC_ALGORITHMS: frozenset[str] = frozenset(
    {
        "EdDSA",
        "ES256",
        "ES384",
        "ES512",
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
    }
)

# Better Auth's `jwt` plugin signs with EdDSA by default and its published JWKS
# does carry `alg`. This map covers the case where it does not -- a JWK is only
# REQUIRED to carry `kty`, so inferring from the curve is the difference between
# working against a spec-minimal key set and working against one specific
# vendor's current output.
_CRV_TO_ALG: dict[str, str] = {
    "Ed25519": "EdDSA",
    "P-256": "ES256",
    "P-384": "ES384",
    "P-521": "ES512",
}

# 60s of clock skew. Render's instances and the browser are not synchronised,
# and a token minted one second in this API's future is not an attack.
DEFAULT_LEEWAY_SECONDS = 60

# How long a fetched key set is trusted before it is re-read. Long, because the
# real freshness mechanism is `UnknownKeyId` forcing a refresh on demand -- a
# short TTL would poll a healthy endpoint to catch a rotation that announces
# itself anyway.
DEFAULT_JWKS_TTL_SECONDS = 3600

_BEARER_SCHEME = "bearer"


class InvalidToken(Exception):
    """The token is not acceptable. Never carries the token itself.

    One exception type for every rejection reason on purpose. The caller's
    correct response is identical in all cases -- 401, sign in again -- and the
    distinction between "expired", "wrong audience" and "forged" is precisely
    the information an attacker is probing for. `deps.py` turns any of these
    into the same 401 with the same body, the same way `current_user` already
    collapses missing, expired, revoked and deactivated.
    """


class UnknownKeyId(InvalidToken):
    """The `kid` is not in the key set we hold.

    Split out because it is the ONE rejection that may be this API's fault
    rather than the caller's: Better Auth rotates signing keys, and a cached
    JWKS that predates a rotation rejects every legitimately-issued new token,
    permanently, until the TTL expires. Callers retry once against a forced
    refresh -- see `JwksCache.get(force=True)`. It stays a subclass so that a
    caller which does NOT special-case it still fails closed.
    """


def extract_bearer(header: str | None) -> str | None:
    """Pull the credential out of an `Authorization` header, or None.

    Returns None rather than raising for every malformed input. An absent or
    unparseable header means "this request did not present a bearer token",
    which is not an error -- the cookie path may still authenticate it during
    the cutover, and an anonymous request is legitimate on some routes.

    The scheme is matched case-insensitively because RFC 7235 defines it as
    case-insensitive and clients genuinely vary. The token itself is not
    touched: whitespace inside a JWT is not something to be helpful about.
    """
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != _BEARER_SCHEME:
        return None
    credential = credential.strip()
    return credential or None


def _algorithm_for(jwk: dict[str, Any]) -> str | None:
    """The one algorithm this key may be used with, or None if unusable.

    Read off the KEY, never off the token. This is the line that makes
    algorithm confusion impossible rather than merely unlikely: whatever the
    token's header claims, the algorithm passed to `decode` comes from the key
    set this API fetched over TLS from its own auth service.
    """
    alg = jwk.get("alg") or _CRV_TO_ALG.get(jwk.get("crv", ""))
    if alg in _ASYMMETRIC_ALGORITHMS:
        return alg
    # Covers `alg: "HS256"` on a published key, `alg: "none"`, an unknown curve,
    # and an `oct` (symmetric) key someone put in a JWKS by mistake.
    return None


def verify_token(
    token: str,
    jwks: dict[str, Any],
    *,
    issuer: str,
    audience: str | None = None,
    leeway: int = DEFAULT_LEEWAY_SECONDS,
) -> dict[str, Any]:
    """Verify signature, expiry, issuer and audience. Return the claims.

    A pure function of `(token, jwks)` -- no network, no clock beyond `time`, no
    database. That is what lets `auth_check.py` mint a real Ed25519 keypair and
    exercise the forgery cases offline, and it is why the cache below is a
    separate object rather than a parameter here.

    Raises `InvalidToken` (or `UnknownKeyId`) for every failure. Never returns a
    partially-verified result and never returns None -- a caller that forgets to
    catch gets an exception, not a falsy value it might treat as anonymous.
    """
    # The header is attacker-controlled and is read as a ROUTING hint only: it
    # selects which key to try. Nothing in it is trusted as a fact.
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.PyJWTError as exc:
        raise InvalidToken("malformed token header") from exc

    kid = header.get("kid")
    if not kid:
        # Refused rather than tried against every key in the set. Trying them
        # all would work, and it turns a key set into an oracle: it lets a token
        # be accepted under a key its issuer never selected, which is the
        # property that makes rotation and revocation meaningful.
        raise InvalidToken("token carries no key id")

    matching = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid),
        None,
    )
    if matching is None:
        raise UnknownKeyId(f"no key with kid {kid!r} in the key set")

    algorithm = _algorithm_for(matching)
    if algorithm is None:
        raise InvalidToken("key set contains no usable asymmetric algorithm")

    try:
        key = pyjwt.PyJWK.from_dict(matching, algorithm=algorithm).key
    except pyjwt.PyJWTError as exc:
        raise InvalidToken("key could not be loaded") from exc

    options: dict[str, Any] = {"require": ["exp", "iss", "sub"]}
    if audience is None:
        # NOT redundant, and the reason is a pyjwt behaviour that reads exactly
        # backwards from the intent. `audience=None` does not mean "do not check
        # the audience" -- `_validate_aud` raises InvalidAudienceError when the
        # TOKEN carries an `aud` the caller did not ask for. So a verifier
        # configured with no audience rejects every token that has one.
        #
        # Better Auth always sets `aud` (to its baseURL), and
        # `BETTER_AUTH_AUDIENCE` is empty by default, so that is the SHIPPED
        # combination: a correct token, a correct verifier, and a 401 for every
        # signed-in user. Found in the browser, not by the harness -- every case
        # paired a token carrying `aud` with a caller that passed one, so the
        # pairing was never varied. `auth_check.py` case 12b is that gap closed.
        options["verify_aud"] = False
    try:
        return pyjwt.decode(
            token,
            key,
            # Exactly one algorithm, resolved from the key. This single keyword
            # is what rejects `alg: none` and the HS256 confusion above.
            algorithms=[algorithm],
            issuer=issuer,
            # Paired with `verify_aud: False` above when this is None. On its
            # own it would REJECT any token carrying an `aud`; see the options
            # block for why that is the opposite of what it reads like.
            audience=audience,
            leeway=leeway,
            options=options,
        )
    except pyjwt.PyJWTError as exc:
        # Deliberately collapsed. `exc` distinguishes expired from forged and
        # that distinction goes to the log, never to the client.
        raise InvalidToken(str(exc)) from exc


class JwksCache:
    """Fetch and hold the auth service's public keys.

    Separate from `verify_token` so that verification stays offline and
    testable, and so the network failure mode lives in one place.

    Concurrency matters here in a way it usually does not. Render's starter plan
    runs a SINGLE uvicorn worker, so a cold cache under load means every
    in-flight request racing to fetch the same document; the lock collapses that
    to one fetch while the rest await it. This is the same class of problem as
    the blocking-SDK deferral recorded in CLAUDE.md, arriving somewhere it can
    actually be fixed cheaply.
    """

    def __init__(self, url: str, *, ttl: int = DEFAULT_JWKS_TTL_SECONDS) -> None:
        self._url = url
        self._ttl = ttl
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return self._url

    def _fresh(self) -> bool:
        return self._keys is not None and (time.monotonic() - self._fetched_at) < self._ttl

    async def get(self, *, force: bool = False) -> dict[str, Any]:
        """The key set, fetching it if stale or if `force` is set.

        `force=True` is the rotation path: a caller that got `UnknownKeyId`
        retries once through here. It is deliberately not automatic inside
        `verify_token`, because an unauthenticated caller can put any `kid` in a
        header, and a refresh triggered by that would be a free way to make this
        API hammer its own auth service.
        """
        if self._fresh() and not force:
            assert self._keys is not None
            return self._keys

        async with self._lock:
            # Re-checked inside the lock: while awaiting it, another request may
            # already have done the work. Without this the lock serialises the
            # stampede rather than collapsing it.
            if self._fresh() and not force:
                assert self._keys is not None
                return self._keys

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(self._url)
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._keys is not None:
                    # Serve the stale set rather than 401ing every signed-in
                    # user because the auth service is redeploying. Keys are
                    # valid until rotated, so a stale set is usually still
                    # correct -- and if it is not, the failure is a 401 either
                    # way. Failing open on FRESHNESS is not failing open on
                    # VERIFICATION: every token is still checked against a real
                    # signature.
                    return self._keys
                raise InvalidToken("key set unavailable") from exc

            if not isinstance(payload, dict) or not payload.get("keys"):
                raise InvalidToken("key set is empty or malformed")

            self._keys = payload
            self._fetched_at = time.monotonic()
            return payload
