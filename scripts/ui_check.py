"""
UI check -- the layout assertions, measured rather than eyeballed.

`agentic_check.py` proves the agent loop end to end; this is its counterpart for
the frontend. It exists because feature 05 specified eight acceptance criteria in
prose and none of them were ever executed, and because the defect feature 07
fixes was invisible to every check that already ran.

    RUN IT
      1. backend:   cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
      2. frontend:  cd frontend && npm run dev          (must be port 5173 -- see below)
      3. this:      python scripts/ui_check.py

    This script runs on the GLOBAL interpreter, not the backend venv -- the same
    split CLAUDE.md already records for `scripts/`. Playwright is not a backend
    dependency and must not become one.

    Port 5173 is not a default, it is a requirement: the backend's `cors_origins`
    is an allow-list, so a second dev server on 5174 is a different origin and
    every request fails CORS. That was worth ten minutes once.

WHY THESE ASSERTIONS

A2 is the reason this file exists, and it is the shape of assertion worth
copying. The bug it catches -- opening `Retrieval parameters` collapsing the chat
panel to 24px with zero visible thread -- threw nothing. No exception, no console
error, no failed request, no horizontal overflow, no React warning. The page
rendered perfectly and the product was not on it.

That is `new features/loop.md` T2 in a module that has never seen a tool: **the
error-shaped check passes while the outcome you wanted is absent.** So every
assertion here is written as "did the goal occur?" -- is the thread taller than
zero, is the composer inside the viewport, are there exactly two grid tracks --
rather than "did an error occur?".

A4-A8 are regression guards. A5 to A8 are feature 05's criteria, running for the
first time; they pass today and their job is to keep passing.

ASCII only in print(). The Windows console codepage mangles anything else, and
this repo has lost three throwaway scripts to that already.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

try:
    from playwright.sync_api import Page, sync_playwright
except ModuleNotFoundError:  # pragma: no cover - environment guard
    print("[FAIL] playwright is not installed on this interpreter.")
    print("       pip install playwright && python -m playwright install chromium")
    sys.exit(2)


FRONTEND = "http://localhost:5173"
API = "http://localhost:8000"

# 1440x900 is the laptop the workshop is run on; 834x1112 is the iPad that sits
# between the two grid modes; 390x844 is the phone the composer-in-viewport rule
# was written for; 320x844 is the narrowest width the overflow rule promises.
DESKTOP = (1440, 900)
TABLET = (834, 1112)
PHONE = (390, 844)
NARROW = (320, 844)


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unrun: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str) -> None:
        if ok:
            self.passed.append(f"{name}: {detail}")
            print(f"  [ok]   {name}  {detail}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  [FAIL] {name}  {detail}")

    def unmeasured(self, name: str, detail: str) -> None:
        """Neither green nor red: the assertion did not run.

        A third outcome, because the two-state version lies in one direction.
        The citation-chip assertion used to read `... if chips else True` -- so
        a thread with no citations in it reported a PASS, on a rule that had not
        been evaluated against anything. That is the same trap `loop.md` 5
        records for context precision scoring 1.000 on a single-chunk corpus:
        not excellent retrieval, retrieval that cannot fail.

        Printed as `[warn]` and counted separately. It does NOT fail the run,
        for the same reason `agentic_check.py` prints `[rate]` rather than
        `[FAIL]` for an upstream refusal -- a red row that means "the fixture
        did not produce the input" sends its reader to debug working code.
        """
        self.unrun.append(f"{name}: {detail}")
        print(f"  [warn] {name}  {detail}  <- NOT MEASURED")


#: The dev-login shim stores `google_sub = "dev|<email>"`, so this identity is a
#: SEPARATE user row from the same address signed in through Google -- which is
#: the property that makes the shim safe, and the reason this harness cannot
#: assume it will find anything. It provisions its own agent below.
DEV_EMAIL = "ui-check@groundwork.local"

#: Named so a human reading the agents list knows what made it and that deleting
#: it is safe. Left behind on purpose rather than torn down: re-provisioning on
#: every run costs a round trip, and the check is about layout, not about setup.
CHECK_AGENT_NAME = "UI check agent"


def ensure_agent(page: Page) -> bool:
    """Guarantee there is an agent to open, creating one if the account is empty.

    Created through the API rather than by driving the create wizard. The wizard
    is four steps of UI this file is not testing, and a layout check that fails
    because a wizard button moved would send its reader to debug the wrong thing.
    `page.request` shares the browsing context's cookies, so this is the same
    authenticated session the assertions run in.
    """
    # Asked of the API, not of the DOM. The first version counted
    # `[data-testid="agent-open"]` after waiting for either that OR the "New
    # agent" button -- and the New agent button is always present, so the wait
    # resolved before the list had rendered and every run concluded the account
    # was empty. It then tried to create the agent it had made a moment earlier
    # and got a 409 for its trouble. The API answers the question that is
    # actually being asked, and answers it without a race.
    listed = page.request.get(f"{API}/api/agents")
    if not listed.ok:
        print(f"  [FAIL] could not list agents: {listed.status}")
        return False

    if len(listed.json()) == 0:
        print(f"  [info] no agent on this account; creating {CHECK_AGENT_NAME!r}")

        templates = page.request.get(f"{API}/api/agent-templates")
        if not templates.ok:
            print(f"  [FAIL] could not list templates: {templates.status}")
            return False
        rows = templates.json()

        # A persona template rather than `from-scratch`, so the bar renders a
        # real persona badge and the settings sheet has a system prompt to show.
        # Falls back to whatever exists rather than hard-coding a slug that may
        # be renamed.
        chosen = next(
            (t for t in rows if t.get("slug") != "from-scratch"), rows[0] if rows else None
        )

        created = page.request.post(
            f"{API}/api/agents",
            data={
                "name": CHECK_AGENT_NAME,
                "description": "Created by scripts/ui_check.py. Safe to delete.",
                **({"template_id": chosen["id"]} if chosen else {}),
            },
        )
        # 409 means a previous run already made it -- the agent this needs
        # exists, which is the only thing being asserted here.
        if not created.ok and created.status != 409:
            print(f"  [FAIL] could not create an agent: {created.status} {created.text()[:200]}")
            return False

    # Outside the branch above, so it also runs on an account that already had
    # an agent but no corpus. One small document, so the Sources rail has rows to render and A8 is
    # checking real controls rather than an empty list. `loop.md` 5 is explicit
    # that a scenario has to make the thing it tests necessary -- a rail with
    # nothing in it cannot fail a tap-target assertion, so it would report a
    # success it never earned. Ingest is a few seconds for a file this size.
    agent_id = page.request.get(f"{API}/api/agents").json()[0]["id"]
    if page.request.get(f"{API}/api/agents/{agent_id}/documents").json() == []:
        print("  [info] uploading one document so the rail has content")
        page.request.post(
            f"{API}/api/agents/{agent_id}/documents",
            multipart={
                "file": {
                    "name": "ui-check-source.md",
                    "mimeType": "text/markdown",
                    # ASCII only, and long enough to chunk into more than one
                    # piece so the chunk count in the rail is not always 1.
                    "buffer": (
                        "# UI check source\n\n"
                        "This file exists so scripts/ui_check.py has a corpus to render "
                        "in the Sources rail. It is safe to delete.\n\n"
                        "## Power\n\nThe platform draws 32.0 kW at peak. Life support and "
                        "thermal control take the largest share. Lighting and crew systems "
                        "take the smallest.\n\n"
                        "## Communications\n\nThe downlink runs in Ka-band. The uplink is "
                        "narrower and is used for commanding rather than for telemetry.\n"
                    ).encode("ascii"),
                }
            },
        )

    ensure_answered_turn(page, agent_id)

    page.reload(wait_until="networkidle")
    page.wait_for_selector('[data-testid="agent-open"]', timeout=15_000)
    return True


def ensure_answered_turn(page: Page, agent_id: str) -> None:
    """Guarantee the thread holds one real answer with citations in it.

    **This exists because an assertion was passing without being evaluated.**
    The citation chip is the most-tapped control in the product and the one
    control allowed to be smaller than 44px -- it is 24x24 with a transparent
    44px `::after`, because a 44px inline box would wreck the line height of any
    paragraph containing it. That exception is exactly the kind of thing a suite
    must keep honest, and it cannot: a thread with no citations in it has no
    chips, and `all(...) if chips else True` reports green.

    So the fixture has to produce the input. One question, asked through the API
    rather than by driving the composer, for the same reason the document is
    uploaded that way -- this file is not testing the composer, and a layout
    check that fails because a send button moved sends its reader to the wrong
    place.

    Idempotent and skipped whenever a conversation already exists, so the model
    call is paid for once per fixture rather than once per run.
    """
    conversations = page.request.get(f"{API}/api/agents/{agent_id}/conversations")
    if conversations.ok and len(conversations.json()) > 0:
        return

    # Retrieval over a corpus that has not finished indexing returns nothing, and
    # an answer with no context has no citations to render -- which would leave
    # the assertion exactly as unmeasured as before, but slower. Ingest is a
    # background job, so this waits for the row to reach a terminal status.
    print("  [info] waiting for ingest before asking a question")
    for _ in range(60):
        rows = page.request.get(f"{API}/api/agents/{agent_id}/documents").json()
        if rows and all(r["status"] in ("ready", "indexed", "failed") for r in rows):
            if any(r["status"] == "failed" for r in rows):
                print("  [warn] a document failed to ingest; the thread may have no citations")
            break
        page.wait_for_timeout(2_000)
    else:
        print("  [warn] ingest did not settle in 120s; asking anyway")

    print("  [info] asking one question so the thread has citations (30-60s)")
    answered = page.request.post(
        f"{API}/api/agents/{agent_id}/ask",
        data={"question": "What is the total power draw of the platform?"},
        # A persona turn is generation-bound: retrieval is under two seconds and
        # the rest is the model writing. 30-60s is normal, so the default 30s
        # request timeout would abort a turn that was working.
        timeout=180_000,
    )
    if not answered.ok:
        print(f"  [warn] the question was not answered: {answered.status} {answered.text()[:160]}")
        return
    body = answered.json()
    print(f"  [info] answered in {body.get('latency_ms')} ms, {len(body.get('citations', []))} citations")


def open_agent(page: Page) -> bool:
    """Land on the first agent's workspace. False if there is nothing to open."""
    page.goto(FRONTEND, wait_until="networkidle")

    if page.locator('[data-testid="dev-login-submit"]').count() > 0:
        print("  [info] not signed in; using the dev-login shim")
        page.fill('[data-testid="dev-login-email"]', DEV_EMAIL)
        page.click('[data-testid="dev-login-submit"]')
        # `networkidle` alone is not enough: the session request settles before
        # React has rendered the dashboard, and a count() taken in that gap reads
        # zero agents on an account that has some.
        page.wait_for_selector(
            '[data-testid="agent-open"], [data-testid="create-agent-toggle"]',
            timeout=15_000,
        )

    if not ensure_agent(page):
        return False

    page.locator('[data-testid="agent-open"]').first.click()
    page.wait_for_selector('[data-testid="agent-shell"]', timeout=15_000)
    # The thread loads over the network; without this the first measurement can
    # land on an empty column and report a healthy-looking zero.
    page.wait_for_timeout(1_500)
    return True


def geometry(page: Page) -> dict:
    """One round trip for every number the assertions need."""
    return page.evaluate(
        """() => {
          const vh = innerHeight, vw = innerWidth;
          const de = document.documentElement;
          const shell = document.querySelector('[data-testid="agent-shell"]');
          const bar = document.querySelector('[data-testid="agent-bar"]');
          if (!shell) return { missing: true };

          const sr = shell.getBoundingClientRect();
          const grid = [...shell.querySelectorAll('div')]
            .find(d => getComputedStyle(d).display === 'grid');

          // Scroll regions the USER experiences inside the chat column.
          //
          // Scoped to `[data-testid=chat-column]` rather than to `section`,
          // because the settings sheet renders `<section>` too and its fields
          // were being counted. `<textarea>` and `<input>` are excluded because
          // a textarea carries `overflow-y: auto` intrinsically -- the composer
          // is not a region you scroll to read the conversation. `clientHeight`
          // filters the closed handout dock, which is `display: none`.
          //
          // NOT filtered on actually overflowing, and that was the second wrong
          // version. "Does it overflow right now" is a fact about the fixture:
          // an agent with an empty thread reported ZERO scroll regions and the
          // assertion failed on a layout with nothing wrong with it. What the
          // criterion means is "the user is never given two nested things to
          // scroll", which is a fact about the STRUCTURE and is true or false
          // whether or not the thread happens to be full today.
          const column = shell.querySelector('[data-testid="chat-column"]');
          const scrollers = column ? [...column.querySelectorAll('*')].filter(e => {
            if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') return false;
            const s = getComputedStyle(e);
            if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') return false;
            return e.clientHeight > 0;
          }) : [];
          // Found by structure, not by "is it scrolling" -- see above. This is
          // the box the old header collapsed to zero, so its height has to be
          // readable even when the conversation is empty.
          const thread = scrollers.find(e => e.className.includes('flex-1'));

          const composer = document.querySelector('[data-testid="chat-input"]');
          const cr = composer ? composer.getBoundingClientRect() : null;

          // Every control, minus the citation chips, which are 24x24 on purpose
          // and carry a 44px transparent ::after -- see index.css. Verified as a
          // hit area rather than waved through.
          const small = [...document.querySelectorAll(
            'button, a[href], input:not([type=range]), textarea, select, [role="button"], summary'
          )].filter(e => {
            const b = e.getBoundingClientRect();
            if (b.width === 0 || b.height === 0) return false;      // display:none
            if (e.classList.contains('gw-chip')) return false;       // measured below
            if (e.classList.contains('sr-only')) return false;
            return b.height < 43.5;                                  // sub-pixel slack
          }).map(e => ({
            testid: e.dataset.testid || null,
            text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 30),
            h: +e.getBoundingClientRect().height.toFixed(1),
          }));

          const chips = [...document.querySelectorAll('.gw-chip')].map(e => {
            const after = getComputedStyle(e, '::after');
            return { w: after.width, h: after.height };
          });

          return {
            vw, vh,
            chromeAboveShell: Math.round(sr.top + scrollY),
            barHeight: bar ? Math.round(bar.getBoundingClientRect().height) : null,
            chromeAboveWorkspace:
              Math.round(sr.top + scrollY) + (bar ? Math.round(bar.getBoundingClientRect().height) : 0),
            shellHeight: Math.round(sr.height),
            gridTracks: grid
              ? getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length
              : null,
            gridTemplate: grid ? getComputedStyle(grid).gridTemplateColumns : null,
            threadVisible: thread ? Math.round(thread.clientHeight) : null,
            threadContent: thread ? Math.round(thread.scrollHeight) : null,
            scrollerCount: scrollers.length,
            composerInViewport: cr ? (cr.top >= 0 && cr.bottom <= vh + 0.5) : null,
            scrollWidth: de.scrollWidth,
            clientWidth: de.clientWidth,
            smallTargets: small,
            chips,
          };
        }"""
    )


def run(headed: bool) -> int:
    results = Results()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": DESKTOP[0], "height": DESKTOP[1]})
        page = context.new_page()

        console_errors: list[str] = []
        #: Gateway failures on the Better Auth proxy, held apart from real ones.
        #: `vite.config.ts` forwards `/api/auth/*` to the Node service on :3000 so
        #: the session cookie is first-party in development the way it is in
        #: production; with that process not running, Vite answers 502 and the
        #: SPA's token probe logs it. Whether that probe fires inside this drive
        #: is a matter of timing, which is why A10 has been seen both green and
        #: red on unchanged code -- and an intermittent red is read as flakiness
        #: and then ignored, so it costs more than no assertion would.
        #:
        #: Bucketed rather than filtered, and rather than failed. That is the
        #: argument `agentic_check.py` makes by printing `[rate]` instead of
        #: `[FAIL]` for an upstream refusal: a suite that goes red because a
        #: service nobody started said no teaches its reader to ignore red.
        #: Dropping the line would be worse than either -- it would hide a real
        #: auth failure behind an environment note.
        gateway: list[str] = []

        def record(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if "favicon" in text.lower() or "[vite]" in text.lower():
                return
            # The URL, not only the sentence, and that is the whole reason this
            # is a function rather than the lambda it replaced. Chromium's
            # message for a failed subresource is "Failed to load resource: the
            # server responded with a status of 502" and names nothing, so the
            # text alone cannot say WHICH request failed -- a row that sends its
            # reader hunting through four files for a request it cannot identify
            # is a row that gets ignored. The URL is also what keeps the
            # exemption narrow: keyed on the text "502" alone, a gateway error
            # from anywhere at all would be excused as this one absent service.
            url = (message.location or {}).get("url") or ""
            line = f"{text} <- {url}" if url else text
            gateway_status = any(code in text for code in ("502", "503", "504"))
            if "/api/auth/" in url and gateway_status:
                gateway.append(line)
            else:
                console_errors.append(line)

        page.on("console", record)

        print("\n== opening the first agent ==")
        if not open_agent(page):
            print("  [FAIL] no agent to open. Create one, or run scripts/agentic_check.py --setup")
            browser.close()
            return 2

        # Cleared AFTER sign-in, and this is scoping rather than suppression.
        # The landing page asks `/api/auth/me` before anyone has signed in and
        # is answered 401, which is the app correctly discovering there is no
        # session -- it is the expected behaviour of the screen, not an error on
        # any screen under test. Filtering it by matching its text would also
        # hide a genuine 401 later; resetting by TIME hides only what happened
        # before the measurements began.
        console_errors.clear()
        # Both buckets, or the row would report on a proxy that was only ever
        # asked for a token by a screen this file does not test.
        gateway.clear()

        # -- A1 / A3 -----------------------------------------------------------
        # The same fact from both directions. Keeping both means a regression
        # that moves the boundary is attributed to the side that moved, instead
        # of to whichever assertion happened to be written first.
        print("\n== 1440x900, disclosures closed ==")
        page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
        page.wait_for_timeout(400)
        g = geometry(page)

        results.check(
            "A1 chrome above the workspace <= 140px",
            g["chromeAboveWorkspace"] <= 140,
            f"{g['chromeAboveWorkspace']}px (nav {g['chromeAboveShell']} + bar {g['barHeight']}), was 576",
        )
        results.check(
            "A3 workspace >= 700px",
            g["shellHeight"] >= 700,
            f"{g['shellHeight']}px of {g['vh']}, was 324",
        )
        results.check(
            "A4 exactly two grid tracks",
            g["gridTracks"] == 2,
            f"{g['gridTemplate']!r}, was three at xl",
        )

        # -- A2 ----------------------------------------------------------------
        # THE assertion. Everything the old header could expand into now lives in
        # an overlay, so the workspace must be the same size with it open. Note
        # what is compared: not "did the sheet open" but "is the thread still
        # there", because the old failure was a page that rendered fine with no
        # chat on it.
        print("\n== 1440x900, agent settings sheet open ==")
        before_h = g["shellHeight"]
        before_thread = g["threadVisible"]
        page.click('[data-testid="agent-settings-open"]')
        page.wait_for_timeout(500)
        g_open = geometry(page)

        results.check(
            "A2 workspace unchanged with settings open",
            g_open["shellHeight"] == before_h,
            f"{g_open['shellHeight']}px vs {before_h}px closed (old header: 324 -> 24)",
        )
        results.check(
            "A2 thread still has visible height",
            (g_open["threadVisible"] or 0) > 0,
            f"{g_open['threadVisible']}px of thread visible (old header: 0)",
        )

        # -- A9 ----------------------------------------------------------------
        focused = page.evaluate("() => document.activeElement?.tagName ?? null")
        results.check(
            "A9 focus moves into the sheet",
            focused == "H2",
            f"activeElement is {focused}, expected the dialog heading",
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        returned = page.evaluate(
            """() => document.activeElement?.dataset?.testid ?? document.activeElement?.tagName"""
        )
        results.check(
            "A9 Escape closes and returns focus to the toggle",
            returned == "agent-settings-open",
            f"focus returned to {returned!r}",
        )

        # -- A6 / A8 -----------------------------------------------------------
        print("\n== 834x1112 ==")
        page.set_viewport_size({"width": TABLET[0], "height": TABLET[1]})
        page.wait_for_timeout(500)
        g = geometry(page)
        results.check(
            "A4 two grid tracks at tablet width",
            g["gridTracks"] == 2,
            f"{g['gridTemplate']!r}",
        )
        results.check(
            "A8 every control >= 44px",
            len(g["smallTargets"]) == 0,
            f"{len(g['smallTargets'])} under 44px: {g['smallTargets'][:4]}",
        )

        # -- A5 / A6 -----------------------------------------------------------
        print("\n== 390x844 ==")
        page.set_viewport_size({"width": PHONE[0], "height": PHONE[1]})
        page.wait_for_timeout(500)
        page.evaluate("() => window.scrollTo(0, 0)")
        g = geometry(page)

        results.check(
            "A5 composer inside the viewport at scrollTop 0",
            bool(g["composerInViewport"]),
            f"composerInViewport={g['composerInViewport']}",
        )
        results.check(
            "A6 exactly one scrollable region in the thread column",
            g["scrollerCount"] == 1,
            f"{g['scrollerCount']} scrollers",
        )
        results.check(
            "A8 every control >= 44px on a phone",
            len(g["smallTargets"]) == 0,
            f"{len(g['smallTargets'])} under 44px: {g['smallTargets'][:4]}",
        )
        # The one control allowed to be under 44px, and therefore the one whose
        # exception has to be proved rather than assumed. No chips means the
        # rule was not evaluated -- reported as unmeasured, never as a pass.
        chips = g["chips"]
        if not chips:
            results.unmeasured(
                "A8 citation chips carry a 44px ::after hit area",
                "no chips in this thread, so the 44px exception was not evaluated",
            )
        else:
            bad = [c for c in chips if c["w"] != "44px" or c["h"] != "44px"]
            results.check(
                "A8 citation chips carry a 44px ::after hit area",
                not bad,
                f"{len(chips)} chips, {len(chips) - len(bad)} at 44x44"
                + (f", offenders: {bad[:3]}" if bad else ""),
            )

        # -- A7 ----------------------------------------------------------------
        print("\n== 320x844 ==")
        page.set_viewport_size({"width": NARROW[0], "height": NARROW[1]})
        page.wait_for_timeout(500)
        g = geometry(page)
        results.check(
            "A7 zero horizontal overflow at 320px",
            g["scrollWidth"] <= g["clientWidth"],
            f"scrollWidth {g['scrollWidth']} vs clientWidth {g['clientWidth']}",
        )

        # -- A10 ---------------------------------------------------------------
        # React key warnings arrive as console errors, so this covers both. The
        # dev server's HMR chatter is filtered rather than the whole check being
        # abandoned as noisy -- a check nobody trusts is a check nobody reads,
        # which is the argument the gateway bucket above carries one step
        # further: the exemptions are named and everything else is red.
        print("\n== console ==")
        # Deduplicated, because one broken subresource polled on a timer prints
        # the same line twenty times and buries the second fault underneath it.
        unique = list(dict.fromkeys(console_errors))
        for line in unique:
            print(f"         {line}")
        if gateway:
            results.unmeasured(
                "A10 the Better Auth proxy answered",
                f"{len(gateway)} gateway errors on /api/auth "
                # Wide enough that the URL survives the slice. Chromium's
                # sentence is 79 characters before the path begins, so the
                # narrower cut used elsewhere truncates exactly the half this
                # row exists to print.
                f"({list(dict.fromkeys(gateway))[0][:170]}); "
                "start the Node service on :3000 to measure this road",
            )
        results.check(
            "A10 zero console errors across all four viewports",
            not console_errors,
            f"{len(console_errors)} errors, {len(unique)} distinct"
            + (f": {unique[0][:180]}" if unique else ""),
        )

        browser.close()

    print("\n" + "=" * 62)
    print(
        f"passed {len(results.passed)}   failed {len(results.failed)}"
        f"   not measured {len(results.unrun)}"
    )
    for line in results.failed:
        print(f"  FAILED       {line}")
    # Printed even on a green run. An unmeasured assertion that scrolls past in
    # silence is indistinguishable from one that passed, which is the whole
    # reason this third state exists.
    for line in results.unrun:
        print(f"  NOT MEASURED {line}")
    print("=" * 62)
    return 1 if results.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Watch it run. Useful when an assertion fails and the number alone does not say why.",
    )
    raise SystemExit(run(parser.parse_args().headed))
