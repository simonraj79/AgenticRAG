"""Authentication: Google OIDC identity in, server-side sessions out.

Three modules, deliberately separated by what each one trusts:

    oauth.py    talks to Google. Establishes WHO the caller is, once.
    session.py  owns the credential the browser carries afterwards. Knows
                nothing about Google.
    deps.py     turns that credential back into a `User` on every request.

The seam matters because only the first stage can be stubbed safely. The
`dev-login` route in routes.py replaces oauth.py's answer and nothing else, so
the session machinery a browser test exercises is the same code production runs.

This package is a marker only -- it imports nothing, so that importing
`app.auth.session` does not drag FastAPI routing in with it.
"""
