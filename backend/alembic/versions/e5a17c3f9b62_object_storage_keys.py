"""object storage keys

Revision ID: e5a17c3f9b62
Revises: d4e91c2a7b58
Create Date: 2026-08-17 21:08:44.301117

Two nullable columns, one per table that will hold bytes in Cloudflare R2
(`new features/13-object-storage/PLAN.md` section 3.6).

1. `handouts.storage_key` -- where this handout's file lives in the bucket. NULL
   means "not in R2": a `pending` row whose job has not run, a `failed` row that
   never produced bytes, or any row written before this change set and not yet
   backfilled.

2. `documents.storage_key` -- where this upload's ORIGINAL bytes live. Before
   this change set there were none: `_load_text` extracted text from bytes in
   memory and the original was discarded, which is why a document stuck at
   `processing` has always been unresumable.

**NOTHING IS DROPPED, AND THAT IS THE LOAD-BEARING LINE IN THIS FILE.**

The obvious companion edit is `op.drop_column('handouts', 'content')`, since
moving the bytes is the entire point. It is deliberately absent, for three
reasons, and the third generalises past this migration:

- `settings.storage_route = "postgres"` is a real rollback only while the bytes
  are still there. A route whose fallback has been deleted is not a route.
- A row can legitimately have neither column set. There is one such row in the
  live database today -- a `failed` deck with `content IS NULL` and
  `byte_size = 0` -- so a NOT NULL key would have been wrong on day one, and so
  would any CHECK asserting that exactly one of the two is present.
- It is the blue/green rule this repo already applies to a Pinecone index.
  `scripts/migrate_index.py` builds the replacement ALONGSIDE the original and
  CLAUDE.md's procedure ends "Only then delete the old one by hand". Dropping
  `content` in the same change set that introduces R2 is delete-then-create with
  extra steps.

There is a fourth reason that is about the harness rather than the data, and it
is worth recording because it is a defect this migration AVOIDS rather than
fixes. `scripts/agentic_check.py` S11 asserts the string "handouts.content"
appears in no SQL statement emitted by the list route. Drop the column and that
assertion can never fail again -- it passes forever, measuring nothing, and
nothing in the suite notices that its subject stopped existing. Keeping the
column keeps S11 meaningful; feature 01 additionally rewrites it to assert a
positive property, so that the eventual drop is safe.

**No backfill here.** Migrations run in Render's START command
(`scripts/create_render_services.py:213-218`, "the internal database host does
not resolve from Render's build environment"), so a copy loop in `upgrade()`
would need network egress to R2 during boot and would fail the deploy outright
if the credentials were absent -- turning a data-movement problem into an outage.
`scripts/migrate_bytes_to_r2.py` does it out of band, idempotently, and never
deletes `content`.

Both columns are `Text` rather than `String(n)`. A key is derived
(`app/storage.py`), so its length is bounded by construction rather than by a
guess, and Postgres stores the two identically -- a length cap here would buy
nothing and would need a migration the first time a key scheme gained a segment.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a17c3f9b62'
down_revision: Union[str, None] = 'd4e91c2a7b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, so no server default is needed and no existing row is rewritten.
    # NULL already means "not in object storage", which is true of every row that
    # exists at the moment this runs -- the same property that made
    # d4e91c2a7b58 need no backfill, and for the same reason: the default IS the
    # pre-existing behaviour.
    op.add_column('handouts', sa.Column('storage_key', sa.Text(), nullable=True))
    op.add_column('documents', sa.Column('storage_key', sa.Text(), nullable=True))


def downgrade() -> None:
    # Drops the pointers, not the objects. Anything already written to R2 stays
    # there and becomes unreachable -- the bucket has no record of which key
    # belonged to which row once these columns are gone.
    #
    # That is survivable ONLY because `content` was never dropped: downgrading
    # with `storage_route=postgres` returns the application to reading bytes it
    # still has. Downgrading after a future migration removes `content` would be
    # data loss, which is the whole argument for keeping the two changes in
    # separate revisions.
    #
    # Run `scripts/migrate_bytes_to_r2.py --orphans` after a downgrade to list
    # what was left behind.
    op.drop_column('documents', 'storage_key')
    op.drop_column('handouts', 'storage_key')
