# 05 — The browser proof

`scripts/download_ui_check.py`. The last feature in the change set, and the only one whose
subject is a **browser saving a file** rather than a server returning bytes.

Contracts consumed by reference into [`PLAN.md`](PLAN.md): §3.4 (the download contract — path,
409, 302, `response-content-disposition`), §3.2 (Playwright is not a backend dependency), §3.8
(this script's row in the script table), §5 A10 (the criterion this file discharges), R-11 (an
R2 outage is `[warn]`, never `[FAIL]`).

---

## What the user gets

Nothing new on the screen. What they get is the guarantee that the thing they already do —
click **Download** on a handout card and open the file — still works after the bytes stop
coming out of FastAPI and start coming out of R2 through a redirect.

That guarantee has never been measured in a browser. It has been measured in jsdom, where there
is no layout and no network, and in `httpx`, where there is no browser.

---

## Why a browser is required, and why no existing layer substitutes

Three layers already touch this route and each is blind to a different half of §3.4.

| Layer | What it proves | What it cannot see |
|---|---|---|
| `HandoutCard.test.tsx:165` | the anchor is rendered and **visible** on a `ready` row | jsdom computes no layout, issues no request and follows no redirect. `expect(...).toBeVisible()` is satisfied by an `<a href>` pointing at a 404 |
| `agentic_check.py` S8 ×4, S8b, S8c, S28, S29 (`httpx`) | the route answers, the **302 is followed** once feature 01 sets `follow_redirects=True`, and the bytes are the artefact | `httpx` is not a browser. It does not honour `download`, does not name a file, does not run an `<img>` load, and has no notion of a save dialog |
| `storage_check.py` 72 | `_safe()` output reaches `response-content-disposition`, CRLF-free | what R2 does with that parameter, and what Chromium does with the header R2 emits |

Four facts in §3.4 are **browser-only by construction**:

1. **The redirect is cross-origin.** `api.ts`'s `downloadUrl` builds an absolute URL on the API
   origin; the 302 sends the browser to `<acct>.r2.cloudflarestorage.com`. Whether the cookie is
   sent to the first hop and *not* to the second, and whether Chromium follows a cross-origin
   redirect during a download at all, are properties of the browser's network stack.
2. **`Content-Disposition` is now emitted by R2, not by FastAPI.** The header the user's filename
   arrives in is produced by a different server, from a query parameter, on the far side of a
   redirect. §1.1's probe measured R2 honouring the override with `curl`; nothing has measured
   Chromium *acting* on it.
3. **The `download` attribute is inert cross-origin** and `HandoutCard.tsx:319-322` says so in as
   many words. Before this change set that comment was a note about a belt beside a brace; after
   it, the brace is the only thing left, because the header now comes from R2. If R2 dropped the
   disposition, the file would be named after the object key — `{handout_id}.pptx` — and *nothing
   below this line would notice*.
4. **The chart thumbnail is an `<img>` pointed at a route that now answers 302 with
   `attachment`.** No non-browser layer loads an image.

`build.md` §7's table of green suites that were wrong is at six rows. Every one of them was found
by a person looking at output. This script is the standing version of that look, for the one
interaction the workshop's users perform on every handout.

---

## What ships

One file, `scripts/download_ui_check.py`, and nothing else. No frontend change (§3.4), no new
route, no new dependency (§3.2).

**It runs on the GLOBAL interpreter, not `backend/.venv`.** `ui_check.py:14-16` states the policy
and it is quoted rather than paraphrased here because it is the reason:

> *This script runs on the GLOBAL interpreter, not the backend venv — the same split CLAUDE.md
> already records for `scripts/`. Playwright is not a backend dependency and must not become one.*

Both structural details of that policy are copied from `ui_check.py`:

- **The guarded import** at `:49-54` — `try: from playwright.sync_api import ... / except
  ModuleNotFoundError:` printing the two-line install hint and `sys.exit(2)`. Exit **2**, distinct
  from 1, because "the harness could not run" is not "the product is broken".
- **The sync API under `sync_playwright()`**, one `chromium.launch(headless=not headed)`, one
  context, one page — not the async API. Every browser harness in this repo is sync and a second
  idiom would make them un-copyable from each other.

And two more from the same file: sign-in through the **dev-login form in the DOM**
(`ui_check.py:263-273` — fill `[data-testid="dev-login-email"]`, click
`[data-testid="dev-login-submit"]`, then wait on a selector, because `networkidle` settles before
React has rendered), and `page.request` for every API call, which shares the browsing context's
cookies so the fixture and the assertions are the same session.

`mention_popup_check.py` is the second precedent and the one that decides this file's *existence*
rather than its shape. `ui_check.py` passes 15/15 without ever rendering the mention popup, so a
separate script had to open it. The same argument applies here: `ui_check.py` never clicks
Download, and a fact that is only true with a download **in flight** cannot be asserted by a suite
that never starts one.

---

## The assertions

Four, discharging §5 **A10**. Ids are `D1`–`D4` so a failure line names this file rather than
colliding with `ui_check.py`'s `A1`–`A10`.

| # | Assertion | How, and why it is the browser's job |
|---|---|---|
| **D1** | Clicking Download produces a **real file** with the right name and the right size | `with page.expect_download() as dl: page.click('[data-testid="handout-download"]')`. Assert `download.suggested_filename` equals `_safe(row["filename"])` and that the saved file's size equals the row's `byte_size`, both read from `GET /api/agents/{id}/handouts` through `page.request` — never from a constant, which goes stale the first time the fixture changes. `suggested_filename` is Chromium's reading of the `Content-Disposition` R2 emitted, so D1 is the only assertion anywhere that proves §3.4's `response-content-disposition` survived the redirect *and* that R-6 did not happen: a dropped `_safe()` shows up here as a filename that is a UUID |
| **D2** | The saved bytes **open as the thing they claim to be** | A `.pptx` is opened with `python-pptx` off disk and asserted to have `>= 1` slide. This is `12/02`'s rule applied one layer further out — *assert the artefact, never the byte count* — and it matters more here than anywhere, because a 302 has a body of its own: a harness comparing sizes alone would be satisfied by a saved copy of Cloudflare's error page if the presign were malformed. **Precondition, not a failure:** `python-pptx` is a backend dependency and this script runs on the global interpreter. Import it inside a `try`, and on `ModuleNotFoundError` report D2 through `Results.unmeasured` — never `[FAIL]`, and never silently skipped |
| **D3** | The chart thumbnail still **renders** after the redirect | `page.evaluate` over `[data-testid="handout-thumb"]`, asserting `naturalWidth > 0` and `complete === true`. `naturalWidth` is readable on a cross-origin image without `crossOrigin` — only canvas is tainted — so this works with the attribute deliberately absent (`HandoutCard.tsx:210-215`). **This is the one a 302 plus `Content-Disposition: attachment` could plausibly break**, and §3.4's *"No frontend change is required"* is an inference from both consumers being URL-only, not a measurement. The audit flagged it **unverified rather than known-good**; D3 is what changes that. Its failure mode is a broken-image icon on every chart card with no console error and no failed request — `loop.md` T2 again |
| **D4** | A **non-ready** handout still answers 409 and does **not** redirect | `page.request.get(downloadUrl, max_redirects=0)` against a `pending` row, asserting `409`. Not 302, not 404. `HandoutCard.tsx:216-218` gates the thumbnail on exactly this and `types.ts:289-300` encodes it as the contract the panel is written against, so a route that started redirecting a pending row to a key with no object behind it would put a broken `<img>` on every spinner card |

**D1 and D3 are a pair, and the pairing is the point** — the same discipline
`route_specialist_check.py` 25/26 uses. D1 proves the response *forces a save*; D3 proves the same
response *still renders inline in an `<img>`*. Either alone is satisfiable by a route that has
broken the other, and the two together are what the single `downloadUrl` in `api.ts:881` is
required to be.

**D2 is deliberately not "the file is non-empty".** `sandbox_check` case 3 (`PK` + `>= 10_000`
bytes) and `agentic_check` S8 (`byte_size > 0`) were both satisfied by a zero-slide presentation
and by 28 bytes of `b"PK\x03\x04 this is not a real pptx"`. That is the sixth row of `build.md`
§7's table; repeating its mistake in the browser layer would make it the seventh.

---

## Preconditions — checked and reported, never assumed

Every one of these is a fact about the machine, not about the product. A harness that treats a
missing fixture as a defect sends its reader to debug working code, which is the argument
`ui_check.py:83-99` makes for the third state and `agentic_check.py` makes for `[rate]`.

| Precondition | How it is checked | If absent |
|---|---|---|
| Playwright installed on this interpreter | the guarded import, `ui_check.py:49-54` | print the install hint, **exit 2** |
| Backend on `:8000` | `page.request.get(f"{API}/api/config")` before launching anything | `[FAIL]` with the uvicorn command — this one *is* a broken run |
| Frontend on **`:5173`** | `page.goto(FRONTEND)` succeeding | `[FAIL]` naming the port. **5173 is a requirement, not a default**: `cors_origins` is an allow-list, so a second Vite instance that auto-incremented to 5174 is a different origin and every request fails CORS while the backend answers `curl` perfectly. `ui_check.py:18-20` records the ten minutes that cost |
| At least one `ready` handout on the agent | `GET /api/agents/{id}/handouts`, filtered on `status == "ready"` | **unmeasured**, with the `agentic_check.py --setup` hint. Do not make one here: a deck is a model call plus a sandbox run, and a browser harness that can take a minute to reach its first assertion is a harness nobody runs |
| A `ready` **chart** handout for D3 | same list, `kind == "chart"` | D3 **unmeasured**. A run with only decks in it has not evaluated the thumbnail rule, and reporting that as green is exactly the `... if chips else True` bug `ui_check.py:83-99` exists to name |
| A **`pending`** handout for D4 | the same list | D4 **unmeasured** rather than skipped. Creating one is cheap — a `POST` answers 202 before the job runs — and doing so is acceptable *provided the row is left for the job to settle*, never forced to a status by hand (§3.5: `_settle` refuses to move a row that is not `pending`) |
| `python-pptx` on the global interpreter | `try: from pptx import Presentation` | D2 **unmeasured**. Measured present on this box 2026-08-17 (1.0.2, miniconda) — a property of this machine, which is precisely why it is checked rather than relied on |

**R2 unavailability is a precondition too, and it is the one R-11 is about.** An expired token
(R-8, 2027-08-17), a deleted bucket or a network fault produces a 403 or a 5xx from the *second*
hop, after the app has behaved correctly. Detect it — a download that never arrives, or a saved
file whose first bytes are XML — and route it to `[warn]`/NOT MEASURED with the status, never to
`[FAIL]`. **A suite that reddens because a provider said no teaches its reader to ignore red**, and
this repo has already paid for that lesson once with Cohere's trial key, where a rate limit and a
code defect printed the same thing.

### The three-state result class

Copied verbatim in shape from `ui_check.py:69-99`: `Results` with `passed` / `failed` / `unrun`,
`check(name, ok, detail)` printing `[ok]` or `[FAIL]`, `unmeasured(name, detail)` printing
`[warn] ... <- NOT MEASURED`. The summary block prints the failed lines **and the unmeasured lines
even on a green run**, because an unmeasured assertion that scrolls past in silence is
indistinguishable from one that passed. Exit code: `1 if results.failed else 0`, with `2` reserved
for the environment guard.

`unrun` does **not** fail the run. §6 says the rest: *a `[warn]`/`unmeasured` row is not a pass*,
so a reader who wants a clean bill reads three numbers, not one.

ASCII only in `print()`. Three throwaway scripts in this repo have died to the Windows console
codepage, most recently on a `§` and a `│` copied out of a repo file — and a handout filename is
model-written text, so `suggested_filename` is exactly the class of string that carries an
em-dash. Print it through `ascii(...)` when it goes into a detail line.

---

## Why this is the last feature

It is the only assertion in the change set that exercises **the whole chain in one act**: a real
session, a real click, FastAPI's authorisation (`_load_owned`), the derived key (§3.3), the presign
with both overrides (§3.4), R2's response, Chromium's save, and `python-pptx` opening the result.
Features 01–04 each prove one link with the others faked; this proves the links are joined.

It also has to be last because it cannot be written first. There is no meaningful browser
assertion about a redirect until something redirects — writing it earlier would produce a case
that goes green against today's `Response(content=...)` and stays green through the migration
without ever changing what it measures, which is how S3 went green twice while proving nothing.

And it is the *step before* the last step, not the last step itself. §6 ends:

> **And then open the page and download a deck by eye.**

`build.md`'s verification phase ends that way for a reason this change set will not be exempt
from. Every harness was green while a model's own tool-call markup was rendering into the answer
text; the deck outline shipped correct, stored, rendered and reachable, behind a disclosure
labelled "CODE" (12/05). D1–D4 make the *next* person's check one command instead of a download
and a PowerPoint. They do not make the first person's check unnecessary.

---

## What this deliberately does not do

- **It does not test upload storage in the browser.** Feature 04 writes original uploads to R2 and
  nothing in the product reads them back — there is no download route for a document and this
  change set does not build one (§7.1). The assertion that an original is retrievable
  byte-identically is A9, in `agentic_check.py` S36, where a byte comparison belongs. A browser
  harness asserting on bytes it can only reach through the API would be `httpx` with a window
  around it.
- **It does not replace `ui_check.py`'s layout assertions.** `ui_check.py` still owns A1–A10 —
  chrome height, grid tracks, the 44px rule, the composer in the viewport, the 320px overflow rule
  — and `mention_popup_check.py` still owns them with the popup open. This file measures one
  interaction and adds no viewport sweep; running it is not a reason to skip either, and §6 lists
  all three.
- **It does not drive the create-handout flow.** Like `ui_check.py`'s fixture, it asks the API for
  what it needs and reports unmeasured when the API has nothing. A browser check that fails because
  a Make-a-deck button moved sends its reader to the wrong module.
- **It does not assert on `Cache-Control`.** R-7 records the loss of `private, no-store` as
  **accepted**, mitigated only by `r2_presign_ttl_s = 300`. An assertion here would either encode
  the loss as correct or fail forever; the honest home for it is the risk register and a PRD item.
- **It does not add a trace event, a status or a column.** §3.9 — nothing about a download is a
  model decision, and this file only reads.
