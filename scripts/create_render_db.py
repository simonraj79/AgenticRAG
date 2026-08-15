"""Provision the Render Postgres instance for this project.

Idempotent: if a database with the target name already exists, it reports its
configuration instead of creating a second one.

Usage:
    python scripts/create_render_db.py --dry-run   # verify auth, show plan
    python scripts/create_render_db.py             # create
    python scripts/create_render_db.py --write-env # fetch conn strings -> .env

Secrets are never printed. Connection strings are written straight into .env.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# --- Configuration. region/databaseName/databaseUser are immutable once created ---
DB_NAME = "agentic-rag-db"
PLAN = "basic_256mb"     # lowest paid flexible tier; legacy tiers rejected for new DBs
REGION = "singapore"     # MUST be explicit — the API defaults to "oregon"
PG_VERSION = "18"        # string, not int
DATABASE_NAME = "agentic_rag"
DATABASE_USER = "agentic_rag"

API = "https://api.render.com/v1"
ROOT = Path(__file__).resolve().parent.parent


def call(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} on {method} {path}\n{detail}")
        raise


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    write_env = "--write-env" in sys.argv

    load_dotenv(ROOT / ".env")
    token = os.getenv("RENDER_API_KEY")
    if not token:
        print("ERROR: RENDER_API_KEY not set in .env")
        return 1

    owners = call("GET", "/owners?limit=20", token)
    if not owners:
        print("ERROR: no owners returned; is the API key valid?")
        return 1
    owner = owners[0]["owner"]
    owner_id = owner["id"]
    print(f"Workspace : {owner.get('name')}  ({owner_id})")

    existing = call("GET", "/postgres?limit=100", token)
    match = next(
        (p["postgres"] for p in existing if p["postgres"]["name"] == DB_NAME), None
    )

    if match:
        print(f"\nDatabase '{DB_NAME}' already exists - not creating another.")
        _report(match)
        if write_env:
            _write_env(match["id"], token)
        return 0

    print(f"\nDatabase '{DB_NAME}' does not exist. Will create:")
    print(f"  plan     : {PLAN}")
    print(f"  region   : {REGION}")
    print(f"  version  : {PG_VERSION}")
    print(f"  db/user  : {DATABASE_NAME} / {DATABASE_USER}")
    print("  NOTE: region, databaseName and databaseUser cannot be changed later.\n")

    if dry_run:
        print("--dry-run set; nothing created.")
        return 0

    created = call(
        "POST",
        "/postgres",
        token,
        {
            "name": DB_NAME,
            "ownerId": owner_id,
            "plan": PLAN,
            "region": REGION,
            "version": PG_VERSION,
            "databaseName": DATABASE_NAME,
            "databaseUser": DATABASE_USER,
        },
    )
    print("Created.\n")
    _report(created)

    pg_id = created["id"]
    print("\nWaiting for the database to become available...")
    for _ in range(40):
        status = call("GET", f"/postgres/{pg_id}", token).get("status")
        if status == "available":
            print("  status: available")
            break
        print(f"  status: {status}")
        time.sleep(15)
    else:
        print("  timed out waiting; check the Render dashboard.")

    _write_env(pg_id, token)
    return 0


def _report(pg: dict) -> None:
    print(f"  id       : {pg.get('id')}")
    print(f"  name     : {pg.get('name')}")
    print(f"  plan     : {pg.get('plan')}")
    print(f"  region   : {pg.get('region')}")
    print(f"  version  : {pg.get('version')}")
    print(f"  status   : {pg.get('status')}")


def _write_env(pg_id: str, token: str) -> None:
    """Fetch connection strings and append to .env WITHOUT printing them."""
    info = call("GET", f"/postgres/{pg_id}/connection-info", token)
    internal = info.get("internalConnectionString", "")
    external = info.get("externalConnectionString", "")

    env_path = ROOT / ".env"
    text = env_path.read_text(encoding="utf-8")
    if "DATABASE_URL=" in text and "DATABASE_URL=\n" not in text:
        print("\n.env already has a DATABASE_URL; leaving it alone.")
        return

    with env_path.open("a", encoding="utf-8") as f:
        f.write("\n# Render Postgres — written by scripts/create_render_db.py\n")
        f.write("# Local dev uses the EXTERNAL url. On Render, set DATABASE_URL to\n")
        f.write("# the INTERNAL one (private network, same region, much faster).\n")
        f.write(f"DATABASE_URL={external}\n")
        f.write(f"DATABASE_URL_INTERNAL={internal}\n")

    print("\nConnection strings appended to .env (values not printed).")
    print(f"  external : {external[:22]}...{'*' * 12}  [{len(external)} chars]")
    print(f"  internal : {internal[:22]}...{'*' * 12}  [{len(internal)} chars]")


if __name__ == "__main__":
    raise SystemExit(main())
