"""Copy handout bytes from Postgres into R2. Idempotent, and never destructive.

Usage:
    backend/.venv/Scripts/python.exe scripts/migrate_bytes_to_r2.py [--dry-run]
    backend/.venv/Scripts/python.exe scripts/migrate_bytes_to_r2.py --verify
    backend/.venv/Scripts/python.exe scripts/migrate_bytes_to_r2.py --orphans

**This never deletes `handouts.content`.** The change set that introduced object
storage deliberately kept that column so `STORAGE_ROUTE=postgres` remains a
working rollback -- blue/green, the same discipline `migrate_index.py` applies to
a Pinecone index, where the old index stays queryable until a human has confirmed
the new one. Dropping the column is a later, separate decision and it is not this
script's to make.

**It is out of band on purpose, and not part of the migration.** Alembic runs in
Render's START command, so a copy loop inside `upgrade()` would need network
egress to R2 during boot and would fail the whole deploy if a credential were
absent -- turning a data-movement chore into an outage. This runs when a human
runs it.

Three modes:

    (default)  copy every row that has bytes and no key
    --verify   read each key back and compare against the column, byte for byte
    --orphans  list objects in the bucket that no row names

`--orphans` is the reconciliation half of the write ordering. Objects are written
before their rows commit, so a process that dies in between leaves a file nothing
will ever name again -- `delete_quietly` covers the cases where Python survives
to run it, and this covers the ones where it did not.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import undefer  # noqa: E402

from app import storage  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.models import Handout  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def _human(n: int) -> str:
    return f"{n:,} bytes" if n < 1_048_576 else f"{n / 1_048_576:.1f} MB"


async def backfill(dry_run: bool) -> int:
    copied = skipped = failed = 0

    async with SessionLocal() as db:
        # `undefer`, because this is the one job that genuinely wants the bytea.
        # Everything else in the codebase is arranged so it never loads --
        # `deferred()` on the column, no `content` field on `HandoutOut` -- and
        # this script is the deliberate exception rather than a leak of it.
        rows = (
            await db.scalars(
                select(Handout)
                .options(undefer(Handout.content))
                .where(Handout.storage_key.is_(None))
                .order_by(Handout.created_at)
            )
        ).all()

        print(f"  {len(rows)} row(s) with no storage key")

        for row in rows:
            if row.content is None:
                # A `pending` row whose job has not run, or a `failed` one that
                # never produced bytes. Both are legitimately keyless and neither
                # is an error -- which is exactly why the column is nullable and
                # why no CHECK asserts that one of the two is present.
                skipped += 1
                continue

            key = storage.handout_key(row.agent_id, row.id, row.mime_type)
            label = f"{row.kind}/{row.filename} ({_human(len(row.content))})"

            if dry_run:
                print(f"    would copy {label} -> {key}")
                copied += 1
                continue

            try:
                # Object first, then the key. Same ordering as the live write
                # path: a re-run finds the row still keyless and simply puts the
                # same bytes at the same derived key again, which is why this is
                # safe to interrupt at any point.
                await asyncio.to_thread(
                    storage.put_object, key, row.content, row.mime_type
                )
                row.storage_key = key
                await db.commit()
                print(f"    copied {label}")
                copied += 1
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                print(f"    FAILED {label}: {exc}")
                failed += 1

    print(f"\n  copied={copied} skipped(no bytes)={skipped} failed={failed}")
    return 1 if failed else 0


async def verify() -> int:
    """Read every key back and compare. The only mode that can fail loudly."""
    mismatched = missing = ok = 0

    async with SessionLocal() as db:
        rows = (
            await db.scalars(
                select(Handout)
                .options(undefer(Handout.content))
                .where(Handout.storage_key.is_not(None))
                .order_by(Handout.created_at)
            )
        ).all()

        print(f"  {len(rows)} row(s) with a storage key")

        for row in rows:
            try:
                stored = await asyncio.to_thread(storage.get_object, row.storage_key)
            except Exception as exc:  # noqa: BLE001
                print(f"    MISSING {row.filename}: {exc}")
                missing += 1
                continue

            # Compared against the column where it survives, and against
            # `byte_size` where it does not. `byte_size` is the weaker check and
            # is used only as a fallback -- this repo has already paid twice for
            # trusting a byte count as evidence that a file is what it claims,
            # which is what the whole robust-handouts change set was about.
            if row.content is not None:
                good = stored == row.content
            else:
                good = len(stored) == (row.byte_size or -1)

            if good:
                ok += 1
            else:
                print(
                    f"    MISMATCH {row.filename}: "
                    f"r2={len(stored)} db={len(row.content) if row.content else '-'}"
                )
                mismatched += 1

    print(f"\n  ok={ok} mismatched={mismatched} missing={missing}")
    return 1 if (mismatched or missing) else 0


async def orphans() -> int:
    """Objects in the bucket that no row names.

    Read-only. It prints, it does not delete -- an orphan costs storage, while a
    wrongly-deleted object costs a user their file, and those are not
    symmetrical enough to automate.
    """
    async with SessionLocal() as db:
        known = {
            key
            for key in (
                await db.scalars(
                    select(Handout.storage_key).where(Handout.storage_key.is_not(None))
                )
            ).all()
        }

    client = storage.get_client()
    paginator = client.get_paginator("list_objects_v2")
    found = orphaned = 0
    total_wasted = 0

    for page in paginator.paginate(Bucket=settings.r2_bucket, Prefix="agents/"):
        for item in page.get("Contents", []):
            found += 1
            if item["Key"] in known:
                continue
            # A document original is legitimately not in `handouts.storage_key`.
            # Only handout keys are checked here; documents are listed for the
            # reader rather than judged.
            if "/handouts/" not in item["Key"]:
                continue
            orphaned += 1
            total_wasted += item["Size"]
            print(f"    ORPHAN {item['Key']} ({_human(item['Size'])})")

    print(f"\n  objects={found} orphaned handouts={orphaned} wasted={_human(total_wasted)}")
    if orphaned:
        print("  Delete by hand after checking each one; this script will not.")
    return 0


def main() -> int:
    if not storage.enabled():
        print("STORAGE_ROUTE is not 'r2'; nothing to do.")
        print("This script writes to R2, so it refuses to run on the rollback road.")
        return 1

    print(f"  bucket    : {settings.r2_bucket}")
    print(f"  endpoint  : {settings.r2_endpoint_url}")
    print()

    if "--verify" in sys.argv:
        return asyncio.run(verify())
    if "--orphans" in sys.argv:
        return asyncio.run(orphans())
    return asyncio.run(backfill("--dry-run" in sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
