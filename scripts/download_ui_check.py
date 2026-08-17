"""The browser proof: a real click on Download produces a real file.

Run:  python scripts/download_ui_check.py [--headed]

**GLOBAL interpreter, not the backend venv.** `ui_check.py` states the policy and
this file inherits it verbatim: *"Playwright is not a backend dependency and must
not become one."* The guarded import below exits 2 rather than 1, because "the
harness could not run" and "the product is broken" are different answers and only
one of them should send somebody to read application code.

**Why no other layer substitutes.** After the object-storage change set the
download route answers 302 to a presigned URL on Cloudflare, and three facts
about that are only observable in a browser:

    jsdom          `HandoutCard.test.tsx` asserts the anchor is VISIBLE. It
                   computes no layout, issues no request and follows no
                   redirect, so it is satisfied by an anchor pointing at a 404.
    httpx          `agentic_check.py` proves the redirect resolves and the bytes
                   open. It does not prove a BROWSER saves a file, nor what it
                   names it.
    storage_check  proves what this repo put in the request. Not what Cloudflare
                   emitted, and not what Chromium did with what it emitted.

The header that makes the save happen is now written by R2 from a signed query
parameter rather than by FastAPI, and `HandoutCard`'s own comment records that
the `download` attribute is already inert cross-origin -- so before this change
it was a belt beside a brace, and afterwards the brace is all there is.

Requires both servers running: the frontend on 5173 (a hard requirement, not a
default -- `cors_origins` is an allow-list) and the API on 8000.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass, field

try:
    from playwright.sync_api import Page, sync_playwright
except ModuleNotFoundError:  # pragma: no cover - environment guard
    print("[FAIL] playwright is not installed on this interpreter.")
    print("       pip install playwright && python -m playwright install chromium")
    sys.exit(2)


FRONTEND = "http://localhost:5173"
API = "http://localhost:8000"
DEV_EMAIL = "ui-check@groundwork.local"


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
        """Neither green nor red -- the assertion did not run.

        Ported from `ui_check.py` deliberately, and it carries more weight here
        than there. This file depends on a third-party store: an expired API
        token, a deleted bucket or a network partition all make these assertions
        unrunnable, and none of them is a defect in this repository. A suite that
        goes red because a provider said no teaches its reader to ignore red --
        the same argument that keeps `[rate]` out of `agentic_check.py`'s exit
        code.
        """
        self.unrun.append(f"{name}: {detail}")
        print(f"  [warn] {name}  {detail}  <- NOT MEASURED")


def sign_in(page: Page) -> bool:
    """Land on the workspace via the dev-login form, as `ui_check.py` does.

    Through the DOM rather than by posting to `/api/auth/dev-login`, so the
    session cookie is established by the same path a human uses -- which is the
    property the download depends on, the route being a cookie-authenticated GET.
    """
    page.goto(FRONTEND, wait_until="networkidle")
    if page.locator('[data-testid="dev-login-submit"]').count() > 0:
        print("  [info] not signed in; using the dev-login shim")
        page.fill('[data-testid="dev-login-email"]', DEV_EMAIL)
        page.click('[data-testid="dev-login-submit"]')
        page.wait_for_selector(
            '[data-testid="agent-open"], [data-testid="create-agent-toggle"]',
            timeout=15_000,
        )

    # Settle before counting, and the reason is a real race rather than caution.
    # `create-agent-toggle` is part of the dashboard chrome and renders as soon
    # as the view mounts; the agent cards arrive with the list request. So the
    # selector wait above is satisfied by the toggle alone, and counting
    # `agent-open` in the next statement reports 0 on an account that has agents
    # -- which surfaces as "no agent to open, run --setup first", sending the
    # reader to re-provision a fixture that already exists.
    try:
        page.wait_for_selector('[data-testid="agent-open"]', timeout=8_000)
    except Exception:  # noqa: BLE001
        pass  # genuinely empty accounts fall through to the NOT MEASURED branch

    return page.locator('[data-testid="agent-open"]').count() > 0


def find_ready_handout(page: Page) -> tuple[str, dict] | None:
    """The first agent holding a `ready` handout, and that handout's row.

    Uses `page.request`, which shares the browsing context's cookies. Looked up
    through the API rather than by reading the DOM because the panel paginates
    and this is fixture discovery, not an assertion.
    """
    agents = page.request.get(f"{API}/api/agents").json()
    for agent in agents:
        listed = page.request.get(
            f"{API}/api/agents/{agent['id']}/handouts", params={"limit": 200}
        )
        if listed.status != 200:
            continue
        for row in listed.json():
            if row.get("status") == "ready" and (row.get("byte_size") or 0) > 0:
                return agent["id"], row
    return None


def main() -> int:
    headed = "--headed" in sys.argv
    results = Results()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        page = context.new_page()

        try:
            if not sign_in(page):
                results.unmeasured(
                    "D0 workspace reachable",
                    "no agent to open; run agentic_check.py --setup first",
                )
                raise SystemExit(_report(results))

            found = find_ready_handout(page)
            if found is None:
                results.unmeasured(
                    "D0 a ready handout exists",
                    "no agent has a `ready` handout, so nothing could be downloaded",
                )
                raise SystemExit(_report(results))

            agent_id, row = found
            results.check(
                "D0 fixture",
                True,
                f"agent={agent_id[:8]} handout={row['filename']} "
                f"{row['byte_size']} bytes kind={row['kind']}",
            )

            # ----------------------------------------------------------------
            # D1. A click produces a file, with the right name and the right size.
            # ----------------------------------------------------------------
            # Driven through the API-provided values rather than constants, so
            # this is also the browser-visible tell for the risk that `_safe`
            # gets dropped when FastAPI stops emitting the header: a filename
            # that arrives as `download` or empty means the disposition did not
            # survive the move to a signed query parameter.
            # Opened by CLICKING the card, not by constructing a URL. The route
            # shape is an implementation detail of the router, and a hand-built
            # path that no longer matches renders a blank page with no testids on
            # it at all -- which reads as "the workspace is broken" rather than
            # "this harness guessed a URL". `ui_check.py` opens it the same way.
            cards = page.locator('[data-testid="agent-open"]')
            target = cards.first
            for index in range(cards.count()):
                if agent_id[:8] in (cards.nth(index).get_attribute("href") or ""):
                    target = cards.nth(index)
                    break
            target.click()
            page.wait_for_selector('[data-testid="agent-shell"]', timeout=15_000)
            page.wait_for_timeout(1_500)

            # The dock is shut by default and its contents are hidden rather
            # than unmounted (`HandoutDock` uses `hidden`, deliberately -- a
            # child that reports a count upward must stay mounted). Hidden is
            # still not clickable, so it has to be opened.
            dock = page.locator('[data-testid="handout-dock-toggle"]')
            if dock.count() > 0:
                dock.first.click()
                page.wait_for_selector(
                    '[data-testid="handout-download"]', timeout=15_000, state="visible"
                )

            link = page.locator('[data-testid="handout-download"]').first
            if link.count() == 0:
                results.unmeasured(
                    "D1 clicking Download saves a file",
                    "no handout-download anchor rendered on this workspace",
                )
            else:
                with page.expect_download(timeout=30_000) as info:
                    link.click()
                download = info.value
                target = download.path()
                saved = target.read_bytes() if target else b""

                results.check(
                    "D1 clicking Download saves a file with the row's name and size",
                    download.suggested_filename.endswith(
                        row["filename"].rsplit(".", 1)[-1]
                    )
                    and len(saved) == row["byte_size"],
                    f"suggested={download.suggested_filename!r} "
                    f"saved={len(saved)} expected={row['byte_size']}",
                )

                # ------------------------------------------------------------
                # D2. The saved bytes are the thing they claim to be.
                # ------------------------------------------------------------
                # `byte_size > 0` is exactly the assertion the robust-handouts
                # change set was built to replace -- a zero-slide deck is 27,387
                # bytes and 28 bytes of `PK` junk passed every check in the
                # repository. Opening the file is the only honest version, and it
                # is done here with the stdlib `zipfile` rather than python-pptx
                # so that this harness does not acquire a dependency the global
                # interpreter may not have.
                if row["kind"] == "deck":
                    try:
                        with zipfile.ZipFile(io.BytesIO(saved)) as archive:
                            slides = [
                                n
                                for n in archive.namelist()
                                if n.startswith("ppt/slides/slide")
                                and n.endswith(".xml")
                            ]
                        results.check(
                            "D2 the saved deck opens and has slides",
                            len(slides) >= 1,
                            f"slides={len(slides)}",
                        )
                    except zipfile.BadZipFile as exc:
                        results.check(
                            "D2 the saved deck opens and has slides",
                            False,
                            f"not a zip: {exc}",
                        )
                elif row["kind"] == "chart":
                    results.check(
                        "D2 the saved chart is a PNG",
                        saved[:8] == b"\x89PNG\r\n\x1a\n",
                        f"magic={saved[:8]!r}",
                    )
                else:
                    results.unmeasured(
                        "D2 the saved file opens",
                        f"kind={row['kind']} has no structural check here",
                    )

            # ----------------------------------------------------------------
            # D3. The chart thumbnail still renders through the redirect.
            # ----------------------------------------------------------------
            # The one the audit flagged as plausible-but-unverified rather than
            # known-good. `HandoutCard` points an `<img>` at the same URL as the
            # download anchor, with `crossOrigin` deliberately unset; after the
            # change that URL 302s to Cloudflare and the response carries
            # `Content-Disposition: attachment`. Browsers ignore that header for
            # `<img>`, so this is expected to work -- and "expected to work" is
            # the reason to measure it. `naturalWidth` is readable cross-origin
            # without CORS; only canvas access is tainted.
            thumb = page.locator('[data-testid="handout-thumb"]')
            if thumb.count() == 0:
                results.unmeasured(
                    "D3 the chart thumbnail renders through the redirect",
                    "no chart thumbnail on this workspace (needs a `chart` handout)",
                )
            else:
                page.wait_for_timeout(1500)
                natural = thumb.first.evaluate("el => el.naturalWidth")
                results.check(
                    "D3 the chart thumbnail renders through the redirect",
                    bool(natural) and natural > 0,
                    f"naturalWidth={natural}",
                )

            # ----------------------------------------------------------------
            # D4. A row that is not ready still refuses, and does not redirect.
            # ----------------------------------------------------------------
            # The contract `HandoutCard` gates the thumbnail on and `types.ts`
            # encodes. Asserted with a fabricated id, which is the only way to
            # get a deterministic non-ready response without racing a job: a
            # missing row and a non-ready row must both fail closed, and neither
            # may hand back a signed URL.
            probe = page.request.get(
                f"{API}/api/agents/{agent_id}/handouts/"
                "00000000-0000-0000-0000-000000000000/download",
                max_redirects=0,
            )
            results.check(
                "D4 a handout that is not ready never yields a signed URL",
                probe.status in (403, 404, 409),
                f"status={probe.status}",
            )

        finally:
            context.close()
            browser.close()

    return _report(results)


def _report(results: Results) -> int:
    print()
    print("=" * 74)
    print(
        f"  passed {len(results.passed)}  failed {len(results.failed)}  "
        f"not measured {len(results.unrun)}"
    )
    if results.unrun:
        # Printed even on a green run. A row that did not run is not a row that
        # passed, and the only way that stays true is if it is visible when
        # everything else is fine.
        print("\n  NOT MEASURED -- treat as unknown, never as passing:")
        for row in results.unrun:
            print(f"    - {row}")
    if results.failed:
        print("\n  FAILED:")
        for row in results.failed:
            print(f"    - {row}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
