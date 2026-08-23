# 05 — The admin trajectory surface

> Contracts consumed: [PLAN.md](PLAN.md) §4.5 (API), §4.6 (frontend). Not restated here.

## What the user gets

A **Trajectory** tab in the admin console showing, across every agent and every user, whether
agents are achieving what they were asked and whether they are using their tools the way the
golden set says they should. It is the rubric made apparent to an administrator without opening a
database.

## Technical detail

### The route

`GET /api/admin/agent-trajectory?days=30`, in `backend/app/api/admin.py`, taking `admin: AdminUser`.

Four constraints, each of them an existing `admin_check.py` case rather than a style note — and
the third and fourth are the ones that would break a case if ignored:

1. `AdminUser` in the signature → **case 1 picks the route up automatically** by router
   introspection. Nothing to register.
2. Path begins `/api/admin` → case 1b.
3. **The path parameter is not `{conversation_id}`.** Case 1c asserts exactly one route contains
   it. This route has no path parameter at all.
4. **It does not call `_audit(`.** Case 1e asserts no non-transcript route audits. This reads
   aggregates, and `admin.py`'s module rule is *reading a transcript writes an `audit_log` row;
   aggregates do not.*

It declares a pydantic `response_model` whose per-metric entries are `Measured`, so case 5's
denominator rule applies. It reuses `_spend_columns()` for nothing — there is no spend here — and
**never sums `estimated_cost`**, which case 5c greps the whole module for.

### The one thing that is NOT automatic

**`admin_check.py --live` walks two hardcoded path lists** — the 200 sweep and the R6
non-admin-403 sweep. A route absent from them ships with exactly the defect `--live` exists to
catch: `GET /api/admin/spend` returned **500 on every request** while every offline case was
green, because `date_trunc` with a bound parameter cannot satisfy `GROUP BY`, and in the browser
it surfaced as a **CORS error** naming the wrong subsystem entirely.

So this feature adds the new path to **both** lists, and that is an acceptance criterion rather
than a step in a checklist.

*(Noted in passing, not fixed here: `/api/admin/account` is already in the R6 list and missing
from the 200 sweep, so it is never asserted to return 200. Out of scope — flagged in §8.)*

### The panel

One entry in the `Tab` union, one in `TABS`, one line in the render chain, one component in
`frontend/src/views/Admin.tsx`. It uses `useLoad`, the three-line loading/error/null guard, and
**admin** visual conventions (`rounded-lg`, `bg-slate-900`, `tabular-nums`, `min-h-11`) — borrowing
the *structure* of `Scorecard.tsx` (headline verdict, card row, denominator panel, per-row table),
not its `rounded-xl` / `font-mono` classes.

Three rendering rules, each of which has shipped wrong in this codebase before:

- **`not measured` is not `0`.** A goal-accuracy pass rate over zero authored references renders
  as *not measured*, and `MeasuredTile` already branches on `m.measured === 0`.
- **A binary pass rate is not a mean.** It renders as `7 / 9 achieved`, never as `0.78` in the
  same grammar as faithfulness.
- **`tools_enabled: null` renders as "not recorded"**, distinct from "off".

**`Admin.tsx` currently contains zero `data-testid` attributes.** This panel adds them —
`trajectory-panel`, `trajectory-metric-card`, `trajectory-row`, `trajectory-unmeasured` — so it is
addressable by a browser harness and by Testing Library, which nothing in the console is today.

## Acceptance criteria

| # | Case | Asserts |
|---|---|---|
| E1 | `admin_check.py` 1 | The new route depends on `require_admin` — automatic, but it must be *seen* passing with the route present |
| E2 | `admin_check.py` 1e | Still green: the new route does not call `_audit(` |
| E3 | `admin_check.py` 6 | The response model's metric entries are `Measured`, carrying `measured` and `total` |
| E4 | `admin_check.py` 6b | The module source contains no `sum(ApiUsage.estimated_cost)` — case 5c widened to cover the new code |
| E5 | `admin_check.py` L. (`--live`) | `GET /api/admin/agent-trajectory` returns **200** against the real database. **The case the route is written for** |
| E6 | `admin_check.py` L. R6 (`--live`) | A signed-in non-admin gets **403** from it |
| E7 | `Admin.trajectory.test.tsx` | The panel renders *not measured* for a zero denominator and a `7 / 9` pass rate for a real one. **The pair is the point** — a component that always renders "not measured" passes the first alone |
| E8 | `Admin.trajectory.test.tsx` | `tools_enabled: null` renders "not recorded" and `false` renders "off" |
| E9 | `ui_check.py` A11 | With the fixture identity promoted to admin, the Trajectory tab opens, the panel is present, every control is ≥ 44 px and there is zero horizontal overflow at 320 px |

E7/E8 are the **first frontend tests the admin console has**; there is no `Admin.test.tsx` today.

E9 needs `ui_check.py` to sign in as an admin, which it has never done — it signs in as a
non-admin dev identity and only ever opens the agent workspace. The fixture user's role is
promoted and **restored in a `finally`**, on the `mention_popup_check.py` model.

## What must keep working

- **Every existing admin route and case.** `admin_check.py` offline and `--live` both stay green,
  including case 1c's "exactly one `{conversation_id}` route".
- **The nav gate.** The tab is inside the existing `user.role === "admin"` view; no new access
  path, and the 403 remains the access control rather than the hidden link.
- **`npm run build` stays clean**, and the existing 56 frontend tests stay green.
