"""Authlib registration for Google. One provider, one purpose: identity.

We run the server-side authorization-code flow, so the client secret never
leaves FastAPI and the browser never talks to Google's token endpoint. That is
also why the Google console entry has an Authorized redirect URI (pointing at
this backend) and NO Authorized JavaScript origins -- they are not two places
for the same URL, and the empty one is empty on purpose (PRD section 8).
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth

from app.config import settings

# The OIDC discovery document. Authlib fetches it lazily on first use and reads
# the authorization, token and jwks endpoints out of it, so none of those URLs
# are hardcoded here -- if Google moves one, discovery follows.
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

# Exactly "openid email profile", and `openid` is not optional (PRD section 7).
# Authlib only generates a nonce when the scope contains `openid`, and only
# attaches `token["userinfo"]` when a nonce was stored for the request. Drop it
# and the flow still completes, the token still comes back -- and then the
# callback dies on a bare `KeyError: 'userinfo'` that points at our code rather
# than at the scope string three files away. `email` and `profile` are base OIDC
# scopes, granted without review, which is why the Data Access page in the
# console lists nothing.
GOOGLE_SCOPE = "openid email profile"

oauth = OAuth()

oauth.register(
    name="google",
    server_metadata_url=GOOGLE_DISCOVERY_URL,
    client_id=settings.google_oauth_client_id,
    client_secret=settings.google_oauth_client_secret,
    client_kwargs={"scope": GOOGLE_SCOPE},
)
