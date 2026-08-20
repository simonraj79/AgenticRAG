# 04 — The admin console

Contracts consumed: [PLAN.md](PLAN.md) §3.5 API surface, §3.7 frontend contract.
**Nothing here restates them.**

## What the user gets

An **Admin** entry in the nav, visible only to an admin, opening seven tabs: Overview,
Users, Agents, Conversations, Spend, Evaluations, Audit.

## Technical detail

**No router, consistent with the rest of the app.** `App.tsx` states why one was declined;
admin is a third arm of the same `View` union. The nav entry already existed as an `admin`
*status pill* — it becomes the control rather than gaining a second element beside it.

**Hiding the entry is not access control.** The 403 from `require_admin` is. The entry is
hidden because a link that always fails is bad UI.

**Every number renders with its denominator.** `Measured` carries `value`, `measured` and
`total`; `measured === 0` renders as **"not measured"**, never as the value, because a mean
over nothing is an absent number rather than a small one. **Metering coverage is
deliberately the widest tile on the page** — it says how much of everything else can be
trusted, and at the time of writing it reads *2.6% — 2 of 78 measured, 76 predate metering*.

**Money is formatted to survive being small.** One real turn cost `$0.00048199`;
`toFixed(2)` renders that as `$0.00`, which is the exact failure this feature exists to
prevent, arriving in the last two characters of the pipeline. Below a cent, four significant
figures.

**Reading a transcript is a button, never a hover or a prefetch.** It is the one call that
writes an audit row, and the list says so before you press it.

**A `null` group key renders as "unattributed" in the UI and stays NULL in SQL.** Coercing
it in the query would hide that it is a distinct case — a call metered before the serving
provider was recoverable, or one made outside any scope.

**No chart library.** The daily series is a list of divs with a width percentage. Six
rectangles do not justify a dependency on a static site whose entire config surface is one
backend URL — the same reasoning that kept `three.js` out of the landing scene.

## Acceptance criteria

| id | Harness | Asserts |
|---|---|---|
| **D1** | `cd frontend && npm test` | The existing 46 tests stay green — the console adds no regression |
| **D2** | `cd frontend && npm run build` | Production build clean |
| **D3** | manual, PLAN §5 | Open the console signed in as an admin and read one real cost that is **not zero** |
| **D4** | manual | Every tab renders without a console error, and Spend attributes across all five groupings |

**D3/D4 as measured, 2026-08-20** (dev-login, `role: admin`): Overview showed 15 users /
10 dev identities, 7 agents, 68 conversations, coverage 2.6%, recorded spend `$0.000986`
reconciled against OpenRouter's `$0.84` key-month and `$66.61` account total. Spend by call
kind showed `generation $0.000862 / 4 calls` and `rewrite $0.000124 / 2 calls` — i.e. **the
rewriter is 12.6% of spend**, a fact that did not previously exist anywhere.

## What must keep working

- The nav, the dashboard and the agent workspace are untouched; the only edit to `App.tsx`
  is one union arm, one import and turning the existing pill into a button.
- The `sm:` gate on that pill stays. At 320px the width belongs to the way back and the way
  out, which is the reasoning already written beside it.

## As built — where this was wrong

**The console shipped in a light palette into a dark shell**, and every harness was green:
`npm test` 46/46, a clean production build, no console error. The page rendered perfectly
with `text-slate-900` headings on a `slate-950` background — legible to a linter, invisible
to a reader. It is the same shape as the 24px chat pane in
[07-workspace-shell.md](../07-workspace-shell.md): *an error-shaped check passes while the
thing you wanted silently did not happen*, and only opening the page found it.

The fix was a single-pass regex over the class map. **Sequential `str.replace` calls would
have been wrong** — mapping `text-slate-500 -> text-slate-400` and then
`text-slate-400 -> text-slate-500` in two passes flattens two tiers into one, silently.


**And it happened a second time, in the same file, at the same last step.** Once rerank
metering landed (feature 05), the Spend table rendered rerank as **`$0`**. The backend
coalesces a NULL sum to `0.0` so the JSON type stays a number; the frontend printed it. So a
real expense — Cohere bills in search units and reports no dollar figure — was displayed as
FREE, which is the single failure this entire change set was built to prevent, arriving in
the last render step after every harness was green and after the `$0.00` rounding trap had
already been fixed two paragraphs above.

`priced_calls` is the discriminator and it was already in the payload, put there for exactly
this and then not consulted at the point of use. The row now reads
**`not priced (3 units)`**, and token columns show `--` rather than `0` for a call type that
is not token-billed.

The rule this pair teaches, stated so the third occurrence is caught by a person rather than
by a screenshot: **a backend that coalesces NULL to 0 has moved the "not measured" decision
into the frontend, and the frontend must be told.** Carrying the denominator in the payload
is only half the job; every render site has to branch on it.
