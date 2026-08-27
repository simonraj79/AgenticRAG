"""Keep `--autogenerate` from deleting the authentication system.

Better Auth owns its own tables and creates them through its own CLI
(`npx @better-auth/cli migrate`). They have no SQLAlchemy model, on purpose:
`Base.metadata` is this application's declaration of what it owns, and adding
mirror models for tables a different service in a different language migrates
would make two sources of truth for one schema.

Alembic's autogenerate compares `Base.metadata` against the live database and
treats anything present in the database and absent from the metadata as
something to DROP. Better Auth's five tables are exactly that. So without this
filter, the next unrelated `alembic revision --autogenerate` emits:

    op.drop_table("account")
    op.drop_table("session")
    op.drop_table("user")
    op.drop_table("verification")
    op.drop_table("jwks")

and nothing raises. The migration reviews cleanly, because a DROP for a table
you have never heard of looks like tidy-up rather than deletion. It is a
different failure from the ones this repository already collects, in one
respect worth naming: it does not fire during the change that causes it. It
fires on whoever next adds a column, months later, with no reason to connect
the two.

WHY THE PLURAL MATTERS, AND WHY IT IS THE DANGEROUS PART.

This application's tables are `users` and `sessions`. Better Auth's are `user`
and `session`. There is no collision -- which is lucky, since a collision would
have surfaced immediately as a CREATE TABLE failure during the cutover, i.e. as
a loud error at the moment someone was looking. Instead the two schemas coexist
silently and the only thing that ever notices is autogenerate.

That near-miss is also the trap in the FILTER: any rule loose enough to exclude
`user` by prefix, substring or `startswith` will also exclude `users`, and then
this application's own identity table stops being migrated -- the opposite
failure, equally silent. `auth_check.py` cases 2 and 3 are that pair, and case 3
is the one to read first if a future edit turns them red.

The set is therefore EXACT-MATCH and explicit. Better Auth adds a table per
plugin, so it grows when a plugin is added -- `jwks` is here because the `jwt`
plugin is enabled, and enabling `twoFactor` or `organization` later means adding
their tables here in the same commit as the plugin.
"""

from __future__ import annotations

# Exact table names, never prefixes. See the module docstring.
#
#   user, session, account, verification   Better Auth core
#   jwks                                   the `jwt` plugin, which is what lets
#                                          FastAPI verify a login it cannot
#                                          otherwise see
BETTER_AUTH_TABLES: frozenset[str] = frozenset(
    {
        "user",
        "session",
        "account",
        "verification",
        "jwks",
    }
)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic `include_object` hook: False hides an object from autogenerate.

    Signature is fixed by Alembic (EnvironmentContext.configure). Only `name`
    and `type_` are consulted:

    `type_ == "table"` is checked before the name, so a COLUMN that happens to
    be called "user" -- or an index, a constraint, a unique key -- is never
    filtered. Without that guard this would silently stop migrating any column
    named `user` or `session` anywhere in the schema, which is a much wider
    blast radius than the problem being solved and would be invisible in exactly
    the same way.

    Returns True for everything else, which is Alembic's default behaviour: this
    hook can only ever REMOVE things from consideration, so the safe answer when
    a rule does not apply is always True.
    """
    if type_ == "table" and name in BETTER_AUTH_TABLES:
        return False
    return True
