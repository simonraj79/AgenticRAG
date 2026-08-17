"""Provision the Cloudflare R2 bucket for this project.

Idempotent, in the same sense `create_index.py` is: if the bucket exists it
verifies the configuration against the plan and reports drift rather than
recreating anything. Nothing here deletes.

Usage:  backend/.venv/Scripts/python.exe scripts/create_r2_bucket.py [--dry-run]

**Uses R2_API_TOKEN, and that is a different credential from the one the
application gets.** The token drives `api.cloudflare.com` and can create and
delete buckets; the access-key pair drives the S3 endpoint and can only touch
objects. The access key id happens to BE the token's id, which makes them look
like one credential -- they are not, and the backend is never given the token,
for the same reason RENDER_API_KEY never reaches the deployed service.

See `new features/13-object-storage/PLAN.md` section 3.8.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# APAC, matching the Render backend (singapore) and the Pinecone index
# (ap-southeast-1). R2 takes a coarse location HINT rather than a region, and it
# is a hint: Cloudflare places the bucket and reports where it landed, which is
# why the drift check below reads the answer back instead of trusting the send.
LOCATION_HINT = "apac"

API = "https://api.cloudflare.com/client/v4"


def _fail(message: str) -> int:
    print(f"ERROR: {message}")
    return 1


def _result(response: httpx.Response) -> dict:
    """Cloudflare wraps everything in {success, errors, result}."""
    body = response.json()
    if not body.get("success"):
        errors = "; ".join(
            f"{e.get('code')}: {e.get('message')}" for e in body.get("errors", [])
        )
        raise RuntimeError(errors or f"HTTP {response.status_code}")
    return body.get("result") or {}


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    load_dotenv(ROOT / ".env")

    account = os.getenv("R2_ACCOUNT_ID", "")
    token = os.getenv("R2_API_TOKEN", "")
    bucket = os.getenv("R2_BUCKET", "groundwork-media")

    if not account or not token:
        return _fail(
            "R2_ACCOUNT_ID and R2_API_TOKEN must be set in .env. "
            "R2_API_TOKEN is the cfat_... management token, not the access key."
        )

    headers = {"Authorization": f"Bearer {token}"}
    base = f"{API}/accounts/{account}"

    with httpx.Client(timeout=30.0, headers=headers) as client:
        # 1. The token, before anything that depends on it. A 403 here is a
        #    revoked or expired token and says so; a 403 three calls later reads
        #    as a bucket permission problem.
        try:
            verified = _result(client.get(f"{base}/tokens/verify"))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Token verification failed: {exc}")

        print(f"  token     : {verified.get('status')}")
        expires = verified.get("expires_on")
        if expires:
            # Printed on every run, deliberately. An expired token makes every
            # download 403 at once while the application is provably unchanged
            # and every offline harness stays green -- a failure that cannot
            # report itself, so the date is put where a human will see it.
            print(f"  expires   : {expires}   <- every download 403s after this")

        # 2. Existing buckets. The account is SHARED with another project, so
        #    this listing is also the check that nothing here is about to
        #    collide with a name somebody else is using.
        try:
            listing = _result(client.get(f"{base}/r2/buckets"))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"Could not list buckets: {exc}")

        buckets = {b["name"]: b for b in listing.get("buckets", [])}
        print(f"  account   : {len(buckets)} bucket(s) -- {', '.join(sorted(buckets)) or 'none'}")

        existing = buckets.get(bucket)

        if existing is None:
            if dry_run:
                print(f"\n  DRY RUN: would create {bucket!r} with locationHint={LOCATION_HINT!r}")
                return 0
            try:
                created = _result(
                    client.post(
                        f"{base}/r2/buckets",
                        json={"name": bucket, "locationHint": LOCATION_HINT},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                return _fail(f"Could not create {bucket!r}: {exc}")
            print(f"\n  CREATED {bucket!r}")
            existing = created
        else:
            print(f"\n  {bucket!r} already exists -- verifying, not recreating")

        print(f"    location      : {existing.get('location')}")
        print(f"    storage class : {existing.get('storage_class')}")
        print(f"    created       : {existing.get('creation_date')}")

        _check_drift(existing)

        # 3. THE SECURITY ASSERTION, and the reason this script exists rather
        #    than a one-line curl. A bucket with public access serves every
        #    object to anyone holding the URL, which would delete the per-agent
        #    authorisation that `OwnedAgent` performs on the download route --
        #    the presigned-URL design exists precisely to keep that decision in
        #    FastAPI. R2 buckets are private by default, so this is checking
        #    that nobody has since turned it on.
        try:
            domains = _result(client.get(f"{base}/r2/buckets/{bucket}/domains/managed"))
        except Exception as exc:  # noqa: BLE001
            print(f"\n  [warn] could not read public-access setting: {exc}")
            print("         Verify by hand that r2.dev public access is DISABLED.")
            return 0

        if domains.get("enabled"):
            print("\n  ** PUBLIC ACCESS IS ENABLED. **")
            print("     Every object is readable by anyone with the URL, which")
            print("     removes the authorisation the download route performs.")
            print("     Disable the r2.dev managed domain in the Cloudflare console.")
            return 1

        print("\n  public access : disabled (correct -- objects are reached by presigned URL)")

    return 0


def _check_drift(bucket: dict) -> None:
    """Location is fixed at creation, exactly like a Pinecone index's region."""
    location = (bucket.get("location") or "").lower()
    if location and location != LOCATION_HINT.lower():
        print(
            f"\n  ** DRIFT: location is {location!r}, the plan asks for "
            f"{LOCATION_HINT!r}. **"
        )
        print("     Fixed at creation. Changing it means a new bucket and a re-upload.")
    else:
        print("\n  Configuration matches the plan.")


if __name__ == "__main__":
    raise SystemExit(main())
