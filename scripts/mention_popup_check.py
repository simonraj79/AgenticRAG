"""The mention popup, measured WITH IT OPEN. Needs both servers, like ui_check.py.

    python scripts/ui_check.py            # 15 assertions, popup never rendered
    python scripts/mention_popup_check.py # 17 assertions, popup open

Run with the GLOBAL interpreter, not the backend venv -- playwright lives there,
the same as ui_check.py.

`scripts/ui_check.py` passes 15/15 with the popup shut, because its fixture agent
has no roster and nothing ever types '@'. That is loop.md section 5's trap: a
check that cannot fail reports success. This opens the popup and re-measures the
three assertions it could break.

  A6  exactly one scrollable region inside [data-testid="chat-column"]
  A8  every control >= 44px
  A7  zero horizontal overflow at 320px
  A10 zero console errors

Gives the agent a roster, measures, and puts the roster back.
ASCII only in print().
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

API = "http://localhost:8000"
APP = "http://localhost:5173"
DEV_EMAIL = "ui-check@groundwork.local"
ROSTER = [
    "feynman-explainer",
    "socratic-tutor",
    "polya-coach",
    "quiz-generator",
    "reflective-coach",
]

# Copied from ui_check.py A6/A8 so this measures the SAME thing rather than
# something similar. A divergence here would let the popup pass a weaker test.
GEOMETRY = """
() => {
  const col = document.querySelector('[data-testid="chat-column"]');
  const scrollers = col ? Array.from(col.querySelectorAll('*')).filter(el => {
    const s = getComputedStyle(el);
    const oy = s.overflowY;
    return (oy === 'auto' || oy === 'scroll')
      && el.clientHeight > 0
      && el.tagName !== 'TEXTAREA' && el.tagName !== 'INPUT';
  }).length : -1;
  const sel = 'button, a[href], input, textarea, select, [role="button"], summary, [role="option"]';
  const short = Array.from(document.querySelectorAll(sel))
    .filter(el => !el.classList.contains('gw-chip') && !el.classList.contains('sr-only'))
    .filter(el => el.getBoundingClientRect().height > 0)
    .filter(el => el.getBoundingClientRect().height < 43.5)
    .map(el => (el.getAttribute('data-testid') || el.tagName) + ':' +
               el.getBoundingClientRect().height.toFixed(1));
  const popup = document.querySelector('[data-testid="mention-popup"]');
  const options = document.querySelectorAll('[data-testid="mention-option"]');
  return {
    scrollers,
    short,
    popupVisible: !!(popup && popup.getBoundingClientRect().height > 0),
    optionCount: options.length,
    optionHeights: Array.from(options).map(o => +o.getBoundingClientRect().height.toFixed(1)),
    popupOverflowY: popup ? getComputedStyle(popup).overflowY : null,
    popupScrolls: popup ? popup.scrollHeight > popup.clientHeight + 1 : null,
    docScrollW: document.documentElement.scrollWidth,
    docClientW: document.documentElement.clientWidth,
    activeDescendant: document.querySelector('[data-testid="chat-input"]')
        ?.getAttribute('aria-activedescendant') || null,
    expanded: document.querySelector('[data-testid="chat-input"]')
        ?.getAttribute('aria-expanded') || null,
  };
}
"""

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label, detail))
    print(f"  [{'ok' if ok else 'FAIL'}]   {label}  {detail}")


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        # The listener is attached AFTER sign-in, not filtered on "401". The
        # signed-out screen probes /api/auth/me and gets a 401 by design --
        # ui_check.py says so in as many words -- but blanket-filtering 401
        # would also hide a real one from a later request. Attaching late scopes
        # the assertion to the thing under test instead of weakening it.
        page.goto(APP, wait_until="networkidle")
        if page.locator('[data-testid="dev-login-submit"]').count() > 0:
            print("  [info] signing in with the dev-login shim")
            page.fill('[data-testid="dev-login-email"]', DEV_EMAIL)
            page.click('[data-testid="dev-login-submit"]')
            page.wait_for_selector('[data-testid="agent-open"]', timeout=15_000)

        page.on(
            "console",
            lambda m: errors.append(m.text)
            if m.type == "error" and "favicon" not in m.text and "[vite]" not in m.text
            else None,
        )

        agents = page.request.get(f"{API}/api/agents").json()
        target = next((a for a in agents if a.get("document_count", 0) > 0), None)
        if target is None:
            print("  [FAIL] no agent with documents; ui_check --setup first")
            return 1
        agent_id = target["id"]
        original = target.get("specialists")
        print(f"  [info] agent {agent_id}  roster before: {original}")

        try:
            patched = page.request.patch(
                f"{API}/api/agents/{agent_id}",
                data={"specialists": ROSTER},
                headers={"Content-Type": "application/json"},
            )
            check(patched.ok, "the API accepts a roster", f"status={patched.status}")
            if not patched.ok:
                print("   ", patched.text()[:300])
                return 1

            page.goto(APP, wait_until="networkidle")
            page.locator('[data-testid="agent-open"]').first.click()
            page.wait_for_selector('[data-testid="chat-input"]', timeout=15_000)

            before = page.evaluate(GEOMETRY)
            check(
                not before["popupVisible"],
                "the popup is shut before anything is typed",
                f"scrollers={before['scrollers']} expanded={before['expanded']}",
            )

            page.click('[data-testid="chat-input"]')
            page.type('[data-testid="chat-input"]', "@", delay=40)
            page.wait_for_selector('[data-testid="mention-popup"]', timeout=5_000)
            after = page.evaluate(GEOMETRY)

            check(after["popupVisible"], "the popup OPENS on '@'",
                  f"options={after['optionCount']}")
            check(after["optionCount"] == 5, "all five specialists are offered",
                  f"count={after['optionCount']}")
            check(after["scrollers"] == 1,
                  "A6 still exactly one scrollable region WITH THE POPUP OPEN",
                  f"scrollers={after['scrollers']}")
            check(after["popupOverflowY"] in ("visible", None),
                  "the popup declares no overflow at all",
                  f"overflow-y={after['popupOverflowY']}")
            check(after["popupScrolls"] is False,
                  "and it does not need to scroll",
                  f"scrolls={after['popupScrolls']}")
            check(not after["short"], "A8 every control still >= 44px",
                  f"under={after['short']}")
            check(
                bool(after["optionHeights"]) and min(after["optionHeights"]) >= 43.5,
                "every suggestion row is >= 44px",
                f"heights={after['optionHeights']}",
            )
            check(after["expanded"] == "true", "aria-expanded is true while open",
                  f"expanded={after['expanded']}")
            check(bool(after["activeDescendant"]),
                  "aria-activedescendant names the active option",
                  f"id={after['activeDescendant']}")

            # Filtering, and the '@risk' case that must close it.
            page.type('[data-testid="chat-input"]', "quiz", delay=40)
            filtered = page.evaluate(GEOMETRY)
            check(filtered["optionCount"] == 1, "typing 'quiz' narrows to one",
                  f"count={filtered['optionCount']}")

            for _ in range(4):
                page.keyboard.press("Backspace")
            page.type('[data-testid="chat-input"]', "risk", delay=40)
            risk = page.evaluate(GEOMETRY)
            check(not risk["popupVisible"], "'@risk' matches nothing and closes it",
                  f"visible={risk['popupVisible']}")

            # 320px with the popup open -- the width A7 guards.
            page.keyboard.press("Escape")
            for _ in range(5):
                page.keyboard.press("Backspace")
            page.set_viewport_size({"width": 320, "height": 844})
            page.click('[data-testid="chat-input"]')
            page.type('[data-testid="chat-input"]', "@", delay=40)
            page.wait_for_selector('[data-testid="mention-popup"]', timeout=5_000)
            narrow = page.evaluate(GEOMETRY)
            check(
                narrow["docScrollW"] <= narrow["docClientW"],
                "A7 zero horizontal overflow at 320px WITH THE POPUP OPEN",
                f"scrollW={narrow['docScrollW']} clientW={narrow['docClientW']}",
            )
            check(narrow["scrollers"] == 1, "A6 holds at 320px too",
                  f"scrollers={narrow['scrollers']}")
            check(not narrow["short"], "A8 holds at 320px too",
                  f"under={narrow['short']}")

            check(not errors, "A10 zero console errors", f"errors={errors[:3]}")

        finally:
            restored = page.request.patch(
                f"{API}/api/agents/{agent_id}",
                data={"specialists": original},
                headers={"Content-Type": "application/json"},
            )
            print(f"  [info] roster restored to {original}: status={restored.status}")
            browser.close()

    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    print("=" * 62)
    print(f"passed {passed}   failed {failed}")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
