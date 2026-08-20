# 03 — The admin API, the promotion, and reconciliation

Contracts consumed: [PLAN.md](PLAN.md) §3.5 API surface, §3.6 audit contract, §3.2 the
migration's promotion step. **Nothing here restates them.**

## What the user gets

`admin@example.com` can read every user, every agent, every conversation in full, every
evaluation run, and what all of it cost — grouped by user, agent, model, call kind or
serving provider.

## Technical detail

**No new authorisation primitive.** `require_admin` and `AdminUser` were built with the
initial schema (`app/auth/deps.py:69,83`) and had **zero callers**; the docstring already
names this feature as their intended consumer. They are used unchanged.

**These routes invert the invariant every other route relies on.** Elsewhere tenancy is
structural — the agent id is in the path and `owned_agent` resolves it, so no request can be
expressed for someone else's data. Crossing that boundary is the whole point here, so
`AdminUser` is doing the entire job alone. `admin_check.py` case 1 asserts the dependency on
every route **by introspecting the FastAPI route table**, because reviewing for it once is a
thing a person did on a Tuesday, not a control.

**Aggregates use correlated scalar subqueries, never joins.** Joining `agents`,
`conversations`, `queries` and `api_usage` onto one user row multiplies them together and
every count returns inflated by the others' cardinality — a bug that renders as a plausible
number.

**The transcript route is the only one that audits.** It is the only one that returns
another person's words. Aggregates deliberately do not: a row per dashboard render would
bury the reads that actually exposed someone's text. The audit metadata records
`self_read`, so reads that crossed a boundary can be filtered from reads that did not.

**`/api/admin/account` is the only external check this system has.** Everything else is our
arithmetic over our own rows; if the meter silently stopped, every number would fall quietly
and look like a quiet week. It reports the two figures side by side and says in the payload
why they are not expected to match — this key also serves work outside Groundwork
(`total_usage: 66.61` account-wide against `usage_monthly: 0.83` on the key). Provider
failures are reported, never raised: a page that 500s on an outage teaches its reader to
ignore the page.

**The promotion is one migration step, `lower(email) = ANY(:emails)`, with no narrowing
predicate.** See [00-AUDIT.md](00-AUDIT.md) §3.6. It printed `promoted 2 user row(s)`.

## Acceptance criteria

| id | Harness | Asserts |
|---|---|---|
| **C1** | `scripts/admin_check.py` case 1 | Every route on the admin router depends on `require_admin`, by route-table introspection |
| **C2** | `scripts/admin_check.py` case 2 | A plain user is refused **403**, not 401 — a 401 sends the frontend into a login loop |
| **C3** | `scripts/admin_check.py --live` | A signed-in **non-admin** gets 403 from *every* route (R6) |
| **C4** | `scripts/admin_check.py --live` | Reading a transcript writes **exactly one** `audit_log` row whose action is `ADMIN_READ_ACTION` |
| **C5** | `scripts/admin_check.py` case 3 | Promotion matches on `lower(email)` with no `LIMIT` and no `google_sub` predicate (R7) |
| **C6** | `scripts/admin_check.py` case 4 | `app/auth/deps.py` never reads `admin_emails` — email stays off the request path |
| **C7** | `scripts/admin_check.py --live` | Every route returns 200; an unknown conversation is **404**; an unknown `group_by` is **422**, never silently defaulted |

**C1's introspection walks nested dependencies**, so a future sub-router carrying the
dependency at router level still counts.

## What must keep working

- **`require_admin` is unchanged, byte for byte.** The promotion changed data, not the
  authorisation path. C6 is the tripwire.
- **Dev-login must reach the console.** Verified in the browser: `POST /api/auth/dev-login`
  for `admin@example.com` returns `role: "admin"`. Without this the console is testable only
  by a human at a Google consent screen, which is the hole dev-login exists to fill.
- Every other route in the app keeps its structural tenancy. Nothing here touches
  `owned_agent`.

## As built — where this was wrong

**`admin_check.py` was entirely green while `GET /api/admin/spend` returned 500 on every
request.** `func.date_trunc("day", ...)` renders the interval as a bind parameter, and
Postgres then refuses `GROUP BY` on it:

```
column "api_usage.created_at" must appear in the GROUP BY clause
```

Two things worth carrying. **In the browser it surfaced as a CORS error** — the middleware
never reached the response because the handler had already raised — which points at
precisely the wrong subsystem. And an offline harness reads source and introspects routes;
it cannot execute SQL, so **a query that compiles and does not run is invisible to it.**
That is the seventh entry in build.md §7's table, and it is why `--live` exists.
