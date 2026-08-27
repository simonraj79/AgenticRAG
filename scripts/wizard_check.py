"""
The create-agent wizard, measured OPEN. Needs both servers, like ui_check.py.

    python scripts/ui_check.py            # 15 assertions, the wizard never opened
    python scripts/wizard_check.py        # this file, the wizard open and driven
    python scripts/wizard_check.py --live # + W10, which WRITES one agent and removes it
    python scripts/wizard_check.py --cleanup   # sweep rows a killed run left behind

Run with the GLOBAL interpreter, not the backend venv -- playwright lives
there, the same split ui_check.py and mention_popup_check.py already record.

Port 5173 is a requirement and not a default: `cors_origins` is an allow-list,
so a second Vite instance on 5174 is a different origin and every request from
it fails CORS on a backend that is running perfectly.

WHY THIS FILE EXISTS

`scripts/ui_check.py:114-121` says in as many words that it will not open this
component -- it creates its fixture agent through the API precisely so a layout
check never fails because a wizard button moved. That decision is right for
that file and it left the create flow with ZERO browser coverage, which is how
a surface reached 511px of a 1440px viewport, 40-49px of horizontal overflow
inside its own panel and a number input rendering as `800Overlap` without one
red row anywhere in the repository.

So this is `mention_popup_check.py`'s shape applied to a second unreachable
surface: the harness that opens the thing, drives it, and measures it in the
state a user is actually in.

WHAT THE ASSERTIONS ARE WRITTEN AGAINST

**Pixels, never class names.** The defect is a CORRECT class in a too-narrow
box: `lg:grid-cols-3` asks "is the WINDOW at least 1024px?", gets yes, and lays
three persona cards into a 511px container. A check that asserted the class was
present would be green on every one of the measured failures, which is
`loop.md` T2 in a module that has never seen a tool -- the error-shaped check
passing while the outcome is absent. Every number below is read off
`getComputedStyle` or `getBoundingClientRect` at a stated viewport.

**Both overflow levels, never just the document.** The drawer panel carries
`overflow-y-auto`, and a box with a non-visible overflow on one axis computes
the other axis to `auto` as well -- so the panel scrolls horizontally and the
DOCUMENT does not. `ui_check.py`'s A7 is therefore structurally incapable of
seeing the 40-49px this file measures. W4 asserts at both levels.

**A gated case is not a pass.** The three-state reporter is `ui_check.py`'s,
copied deliberately rather than reinvented: an assertion that did not run
prints `[warn] ... <- NOT MEASURED` and is listed again at the end even on a
green run. W10 without `--live` is the case that needs it.

WRITING TO THE DATABASE

`DATABASE_URL` points at the live Render Postgres with real users on it, so the
rule this repo already pays for applies: a harness OWNS its subject or it does
not write. Only `--live` writes anything. It signs in as its own identity
(`wizard-check@groundwork.local`, a row the dev-login shim keys as
`dev|<email>` and therefore one that can never collide with a real Google
`sub`), creates agents named `wizard-check <hex>`, prints the name BEFORE the
POST so a row orphaned by a kill is findable by name, counts agents before and
after, and removes what it made in a `finally`. A `finally` does not cover a
killed process, so `--cleanup` does the same sweep on its own and says
"nothing to clean up" when there is nothing.

ASCII only in print(). The Windows console codepage mangles anything else and
this repo has lost three throwaway scripts to it already -- and this file's
docstring is `argparse`'s `description`, so it is terminal output too.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass, field

try:
    from playwright.sync_api import Page, sync_playwright
except ModuleNotFoundError:  # pragma: no cover - environment guard
    print("[FAIL] playwright is not installed on this interpreter.")
    print("       pip install playwright && python -m playwright install chromium")
    sys.exit(2)


FRONTEND = "http://localhost:5173"
API = "http://localhost:8000"

#: Its own identity, not `ui-check@groundwork.local`. Two harnesses sharing one
#: user row makes "count the agents before and after" a measurement of both of
#: them, and this one is the only one that creates and deletes agents while it
#: runs. The shim upserts on email, so the row is created on first use.
DEV_EMAIL = "wizard-check@groundwork.local"

#: Every row this file creates starts with this. `--cleanup` sweeps on the
#: prefix, so a name is the only thing needed to find and remove an orphan --
#: which is the point of printing it before the POST rather than after.
AGENT_PREFIX = "wizard-check "

# 1440x900 is the laptop the workshop is run on and the viewport every baseline
# number in `17-create-agent-ux/MEASUREMENTS.md` was taken at. 834x1112 is the
# iPad that sits between the two grid modes. 390x844 is the phone. 320x844 is
# the narrowest width the zero-overflow rule promises -- and, per the plan, the
# width whose layout is currently the CORRECT one.
DESKTOP = (1440, 900)
TABLET = (834, 1112)
PHONE = (390, 844)
NARROW = (320, 844)

DRIVE_VIEWPORTS = [DESKTOP, TABLET, PHONE, NARROW]

#: W1's floors, per the plan's acceptance table. The desktop one is the whole
#: complaint: 511px of 1440 is 35% of the screen for a four-step task.
W1_FLOOR = {DESKTOP: 720, TABLET: 620}
W1_BASELINE = {DESKTOP: 511, TABLET: 511}

#: W2's floors. A persona card needs more room than a slider pair because it
#: carries a name, a category badge, a role, a description and a pedagogy note;
#: a slider pair carries a label and a 96px number input.
W2_FLOOR = {"persona": 260, "sliders": 200}

W3_VIEWPORTS = [DESKTOP, PHONE]
W12_VIEWPORTS = [DESKTOP, PHONE, NARROW]

#: 44px, with sub-pixel slack, exactly as ui_check.py A8 and
#: mention_popup_check.py measure it. Kept identical on purpose: a different
#: number here would let this surface pass a weaker version of a shared rule.
TAP_TARGET = 43.5

#: W7's floor. Forty characters is what stops a later rewrite satisfying the
#: case by repeating the label as a caption -- "Chunk size, in tokens" is 21.
#: The plan caps `help` at 110 characters and calls it one sentence, so the
#: window this asserts into is 40-110 and is comfortably satisfiable by the
#: copy the contract already specifies.
HELP_FLOOR = 40


def _alias_set(key: str, spoken: list[str]) -> list[str]:
    """Every normalised string that identifies one parameter on screen.

    Three spellings have to be recognised, and only the first exists today.

    1. The label the wizard renders now ("Overlap"), so W7 can FAIL by name
       against current code rather than reporting ten parameters not found --
       "no explanation" and "no such parameter" are different findings and
       collapsing them would send the reader to the wrong file.
    2. The database column (`chunk_overlap`), because the plan's shared
       contract keeps it as "a quiet mono tag" beside the new plain-English
       label. That is the one identifier guaranteed to survive WS3's rewrite of
       every user-facing string.
    3. Both together, in either order, because a label and its tag rendered as
       siblings inside one element produce a single `innerText` of "Overlap
       chunk_overlap". Generated mechanically rather than listed, since which
       order WS3 chooses is not knowable from here.
    """
    base = sorted({*spoken, key.replace("_", " ")})
    pairs = [f"{a} {b}" for a in base for b in base if a != b]
    return sorted({*base, *pairs})


#: The ten keys of the shared contract, each with the labels the CURRENT wizard
#: renders. Order is the order both parameter grids render them in, so a report
#: reads down the screen.
TUNABLE_ALIASES = {
    "chunk_size": _alias_set("chunk_size", ["chunk size"]),
    "chunk_overlap": _alias_set("chunk_overlap", ["overlap"]),
    "splitter": _alias_set("splitter", ["splitter"]),
    "retrieve_k": _alias_set("retrieve_k", ["retrieve k"]),
    "rerank_enabled": _alias_set("rerank_enabled", ["rerank"]),
    "rerank_top_n": _alias_set("rerank_top_n", ["rerank top n"]),
    "score_threshold": _alias_set("score_threshold", ["score threshold"]),
    "max_rewrites": _alias_set("max_rewrites", ["max rewrites"]),
    "tools_enabled": _alias_set("tools_enabled", ["tools"]),
    "max_tool_steps": _alias_set("max_tool_steps", ["max tool steps"]),
}

TUNABLE_KEYS = list(TUNABLE_ALIASES)


# --------------------------------------------------------------------------
# The reporter
# --------------------------------------------------------------------------


@dataclass
class Results:
    """Three states, because the two-state version lies in one direction.

    Lifted from `ui_check.py` rather than rewritten, and the docstring there is
    the argument: an assertion gated on a fixture that did not materialise
    reports a PASS on a rule it never evaluated. `[warn]` rows do not fail the
    run -- a red row meaning "the fixture did not produce the input" sends its
    reader to debug working code, which is why `agentic_check.py` prints
    `[rate]` rather than `[FAIL]` for an upstream refusal -- but they are
    printed again in the summary even when everything else is green, because an
    unmeasured assertion that scrolls past in silence is indistinguishable from
    one that passed.
    """

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
        self.unrun.append(f"{name}: {detail}")
        print(f"  [warn] {name}  {detail}  <- NOT MEASURED")


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

#: One round trip per (viewport, step, tuning mode) for every number the
#: geometry assertions read.
#:
#: Nothing in here names a Tailwind class or a breakpoint. The whole defect is
#: a correct class evaluated against the wrong box, so a class assertion is
#: blind to it by construction -- `lg:grid-cols-3` is present and correct on
#: the persona grid at this moment and the grid is 159px per card.
GEOMETRY_JS = r"""
() => {
  const panel  = document.querySelector('[data-testid="create-agent-panel"]');
  const wizard = document.querySelector('[data-testid="create-agent-wizard"]');
  if (!panel || !wizard) return { missing: true };

  const wr = wizard.getBoundingClientRect();

  // The scroll container the wizard's content lives in, found by WALKING UP
  // from the form rather than by naming the element that has the class today.
  // The plan's WS1 moves the scroll from the drawer panel to a dedicated body
  // region between a fixed header and the child's own sticky footer, so a
  // selector pinned to `[data-testid=create-agent-panel]` would keep measuring
  // an element that has stopped being the scroller and would report a healthy
  // number about the wrong box.
  let body = null;
  for (let node = wizard.parentElement; node && node !== document.body; node = node.parentElement) {
    const s = getComputedStyle(node);
    if (s.overflowY === 'auto' || s.overflowY === 'scroll') { body = node; break; }
  }

  // Every grid inside the wizard, with its RESOLVED track widths. The computed
  // value of `grid-template-columns` on a grid container is used values in px,
  // which is the only reading that answers "how wide is a persona card" -- the
  // declared value would say `repeat(3, minmax(0, 1fr))` at every viewport,
  // including the ones where that resolves to 159px.
  const gridsOf = (host) => [...host.querySelectorAll('*')]
    .filter(el => {
      const d = getComputedStyle(el).display;
      return d === 'grid' || d === 'inline-grid';
    })
    .map(el => {
      const tracks = getComputedStyle(el).gridTemplateColumns
        .split(' ').map(parseFloat).filter(n => !Number.isNaN(n));
      let kind = 'other';
      if (el.querySelector('[data-testid="template-card"]')) kind = 'persona';
      else if (el.querySelector('[data-testid^="param-"]')) kind = 'sliders';
      else if (el.querySelector('dt')) kind = 'facts';
      return {
        kind,
        testid: el.dataset.testid || null,
        width: +el.getBoundingClientRect().width.toFixed(1),
        tracks: tracks.map(n => +n.toFixed(1)),
      };
    });

  // How far the widest descendant sticks out of its host, in px, plus who it
  // is. `scrollWidth > clientWidth` says overflow happened; this says which
  // element to open. `scrollLeft` is added back so the number stays true if
  // the host has already been scrolled sideways.
  const overshoot = (host) => {
    if (!host) return null;
    const hr = host.getBoundingClientRect();
    const out = [];
    for (const el of host.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const over = (r.right + host.scrollLeft) - hr.right;
      if (over > 0.5) {
        out.push({
          over: +over.toFixed(1),
          id: el.dataset.testid || el.id || el.tagName.toLowerCase(),
          text: (el.innerText || '').trim().slice(0, 28).replace(/\s+/g, ' '),
        });
      }
    }
    out.sort((a, b) => b.over - a.over);
    return out.slice(0, 4);
  };

  // Plain `input`, so `input[type=range]` is IN. `ui_check.py` excludes it
  // because it measures screens that have none; the wizard's tuning step is
  // seven of them, and their 44px hit area lives in `.gw-range` in index.css
  // rather than in any class list -- which is exactly the kind of rule that
  // stops being true without anything in the markup changing.
  const SEL = 'button, a[href], input, textarea, select, [role="button"], summary, [role="option"]';
  const controls = [...panel.querySelectorAll(SEL)]
    .filter(el => !el.classList.contains('sr-only'))
    .filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;   // display:none and the closed panel
    })
    .map(el => {
      const r = el.getBoundingClientRect();
      const label = (el.innerText || '').trim();
      return {
        id: el.dataset.testid || el.id || el.tagName.toLowerCase(),
        type: el.getAttribute('type'),
        h: +r.height.toFixed(1),
        w: +r.width.toFixed(1),
        // No letters and no digits on screen means the control is carried by
        // an icon or a glyph, and height alone is then not a tap target. The
        // drawer's close button renders a bare multiplication sign and is the
        // control this clause exists for.
        iconOnly: !/[a-z0-9]/i.test(label),
      };
    });

  const footer = wizard.querySelector(
    '[data-testid="wizard-next"], [data-testid="agent-create-submit"]');
  const fr = footer ? footer.getBoundingClientRect() : null;

  return {
    step: wizard.dataset.step || null,
    vw: innerWidth,
    vh: innerHeight,
    // The number the plan's table calls "content width": how wide the box the
    // wizard draws into actually is. Measured on the form, so it is the answer
    // to "how much room is there" and not "which width token was passed".
    contentWidth: +wr.width.toFixed(1),
    grids: gridsOf(wizard),
    bodyFound: !!body,
    bodyClientHeight: body ? body.clientHeight : null,
    bodyScrollW: body ? body.scrollWidth : null,
    bodyClientW: body ? body.clientWidth : null,
    panelScrollW: panel.scrollWidth,
    panelClientW: panel.clientWidth,
    // Measured against the BODY when there is one, because the body's negative
    // margins put its border box exactly on the panel's padding box -- so this
    // is "how far past the panel edge does anything reach", asked of whichever
    // element currently owns that edge.
    overshoot: overshoot(body || panel),
    docScrollW: document.documentElement.scrollWidth,
    docClientW: document.documentElement.clientWidth,
    footerTestid: footer ? footer.dataset.testid : null,
    footerBottom: fr ? +fr.bottom.toFixed(1) : null,
    controls,
  };
}
"""

#: W7. Does each of the ten parameters have a VISIBLE explanation beside its
#: label, in the mode the user lands in?
#:
#: Written against the structure rather than against a testid, because the
#: testid that would carry it does not exist yet and inventing one here would
#: be this file dictating markup to a workstream that has not been written. The
#: rule is instead: find the element whose entire visible text IS the
#: parameter's name, climb to the largest ancestor that still contains only
#: that parameter, and look inside it for a visible element that is neither the
#: label nor a container of it and that carries at least `floor` characters.
#:
#: The climb terminates on the presence of ANOTHER parameter's label, which is
#: what stops it walking out into the section blurb and reporting the step's
#: introductory paragraph as ten explanations.
EXPLANATION_JS = r"""
({ aliases, floor }) => {
  const wizard = document.querySelector('[data-testid="create-agent-wizard"]');
  if (!wizard) return { missing: true };

  const norm = (s) => (s || '')
    .toLowerCase()
    .replace(/_/g, ' ')
    .replace(/[^a-z0-9 ]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const all = [...wizard.querySelectorAll('*')];

  // The label element for each key: the SMALLEST element whose whole visible
  // text is that parameter's name. Smallest by descendant count, because a
  // wrapper around the label matches the same string whenever the label is its
  // only text, and the wrapper is the wrong anchor to climb from.
  const labels = {};
  for (const key of Object.keys(aliases)) {
    const hits = all.filter(el => visible(el) && aliases[key].includes(norm(el.innerText)));
    hits.sort((a, b) => a.getElementsByTagName('*').length - b.getElementsByTagName('*').length);
    labels[key] = hits[0] || null;
  }
  const anchors = Object.values(labels).filter(Boolean);

  const report = {};
  for (const key of Object.keys(aliases)) {
    const label = labels[key];
    if (!label) { report[key] = { found: false }; continue; }

    // Climb while the ancestor still belongs to this parameter alone.
    let cell = label;
    for (let i = 0; i < 4; i++) {
      const up = cell.parentElement;
      if (!up || up === wizard) break;
      if (anchors.some(a => a !== label && up.contains(a))) break;
      cell = up;
    }

    // The explanation is the SHORTEST qualifying element, so a nested help
    // paragraph is reported rather than the wrapper that happens to contain it
    // plus a value plus a tag.
    let best = null;
    for (const el of cell.querySelectorAll('*')) {
      if (el === label || el.contains(label) || label.contains(el)) continue;
      if (el.classList.contains('sr-only')) continue;
      if (!visible(el) || el.clientHeight <= 0) continue;
      const text = (el.innerText || '').trim();
      if (text.length < floor) continue;
      if (!best || text.length < best.length) {
        best = { length: text.length, height: el.clientHeight,
                 sample: text.slice(0, 48).replace(/\s+/g, ' ') };
      }
    }

    report[key] = {
      found: true,
      label: (label.innerText || '').trim().slice(0, 32),
      cell: cell.tagName.toLowerCase() + (cell.dataset.testid ? '[' + cell.dataset.testid + ']' : ''),
      cellChars: (cell.innerText || '').trim().length,
      help: best,
    };
  }
  return report;
}
"""

#: W10. The ten values the review step is SHOWING, read off the screen the user
#: is looking at when they press Create.
#:
#: The `review-parameters` grid is the primary road; the alias scan is the
#: fallback for after WS4 regroups step 3 and renames it. A third outcome --
#: neither road found anything -- returns null and W10 reports NOT MEASURED
#: rather than passing on an empty comparison, which is the same trap the
#: citation-chip assertion fell into in `ui_check.py`.
REVIEW_JS = r"""
({ aliases }) => {
  const norm = (s) => (s || '')
    .toLowerCase().replace(/_/g, ' ').replace(/[^a-z0-9 ]/g, ' ')
    .replace(/\s+/g, ' ').trim();

  // Exact alias first, then containment of the COLUMN NAME.
  //
  // The label is now "Passage overlap" and the column is rendered beside it as
  // a quiet mono tag, so a dt reads "Passage overlap chunk_overlap" and no
  // exact alias can match it. Falling back to "does this element name the
  // column" is not a loosened check -- it is a match on the one identifier the
  // shared contract promises will survive a rewrite of every user-facing
  // string, which is precisely why the tag is rendered at all. The ten column
  // names share no substring with one another, so containment stays
  // unambiguous: "tools enabled" and "max tool steps" cannot both match one
  // element.
  const byKey = (text) => {
    const n = norm(text);
    for (const key of Object.keys(aliases)) if (aliases[key].includes(n)) return key;
    for (const key of Object.keys(aliases)) {
      if (n.includes(norm(key))) return key;
    }
    return null;
  };

  const out = {};
  const dl = document.querySelector('[data-testid="review-parameters"]');

  // Preferred road: the RAW stored value, published on the element the same way
  // ParamSlider publishes `data-value`.
  //
  // The displayed text is formatted on purpose -- "400 tokens", "At headings"
  // -- because a bare 400 beside "Passage size" is the vocabulary problem this
  // change set exists to fix. Comparing that text to a database column asserts
  // the FORMATTER, not the round trip, and fails as though data were lost the
  // first time a unit is pluralised. `data-value` is what the row holds.
  if (dl) {
    for (const el of dl.querySelectorAll('[data-tunable][data-value]')) {
      const key = byKey(el.getAttribute('data-tunable'));
      if (key) out[key] = (el.getAttribute('data-value') || '').trim();
    }
    if (Object.keys(out).length) return out;
  }

  if (dl) {
    for (const dt of dl.querySelectorAll('dt')) {
      const key = byKey(dt.innerText);
      const dd = dt.parentElement ? dt.parentElement.querySelector('dd') : null;
      if (key && dd) out[key] = (dd.innerText || '').trim();
    }
    if (Object.keys(out).length) return out;
  }

  const section = document.querySelector('[data-testid="create-agent-wizard"]');
  if (!section) return null;
  for (const el of section.querySelectorAll('*')) {
    const key = byKey(el.innerText);
    if (!key || out[key] !== undefined) continue;
    const dd = el.parentElement ? el.parentElement.querySelector('dd') : null;
    const sib = el.nextElementSibling;
    const value = dd ? dd.innerText : (sib ? sib.innerText : null);
    if (value !== null) out[key] = value.trim();
  }
  return Object.keys(out).length ? out : null;
}
"""


# --------------------------------------------------------------------------
# Driving the wizard
# --------------------------------------------------------------------------


def sign_in(page: Page) -> bool:
    """Land on the dashboard as the harness identity."""
    page.goto(FRONTEND, wait_until="networkidle")
    if page.locator('[data-testid="dev-login-submit"]').count() > 0:
        print(f"  [info] signing in with the dev-login shim as {DEV_EMAIL}")
        page.fill('[data-testid="dev-login-email"]', DEV_EMAIL)
        page.click('[data-testid="dev-login-submit"]')
    # Either the list or the create button: an account with no agents renders
    # no cards, and waiting on cards alone would time out on exactly the
    # account this harness prefers to run against.
    page.wait_for_selector(
        '[data-testid="agent-open"], [data-testid="create-agent-toggle"]',
        timeout=15_000,
    )
    return True


def open_wizard(page: Page) -> bool:
    """Open the create drawer, whichever way this account gets there.

    `Dashboard` opens it on its own when the account owns nothing -- "a user who
    owns nothing has no open-my-agent to do, so the form IS the page" -- so
    clicking the toggle unconditionally would be clicking a button that is
    behind an `inert` subtree, which fails rather than being harmless.
    """
    already = page.locator('[data-testid="create-agent-wizard"]').count() > 0
    if not already:
        page.click('[data-testid="create-agent-toggle"]')
    page.wait_for_selector('[data-testid="create-agent-wizard"]', timeout=10_000)
    # The panel animates in over 200ms and the first measurement taken during
    # the transform reads a box that is still partly off-screen.
    page.wait_for_timeout(400)
    return True


def current_step(page: Page) -> int:
    raw = page.get_attribute('[data-testid="create-agent-wizard"]', "data-step")
    return int(raw) if raw else 0


def go_to_step(page: Page, step: int) -> None:
    """Jump through the rail. Requires the flow to have reached step 4 once.

    `wizard-step-N` is on the button when the step is reachable and not current,
    and on a plain span otherwise -- so clicking the step you are already on is
    a click on a `<span>` that does nothing and then a wait that times out.
    """
    if current_step(page) == step:
        return
    page.click(f'[data-testid="wizard-step-{step}"]')
    page.wait_for_selector(
        f'[data-testid="create-agent-wizard"][data-step="{step}"]', timeout=5_000
    )
    page.wait_for_timeout(120)


def set_tuning_mode(page: Page, custom: bool) -> None:
    want = "custom" if custom else "template"
    if page.get_attribute('[data-testid="tuning-mode"]', "data-value") == want:
        return
    page.click(f'[data-testid="tuning-mode-{want}"]')
    page.wait_for_timeout(150)


def unlock_rail(page: Page, name: str) -> None:
    """Fill the required field and walk to step 4 so every step is reachable.

    The rail only turns a step into a button once it has been REACHED
    (`furthest`), which is the whole point of the component -- so a harness that
    wants to measure step 3 has to get there the way a user does, once.
    """
    go_to_step(page, 1)
    page.fill('[data-testid="agent-name-input"]', name)
    # Blur, so `nameTouched` is set by a real blur rather than being forged by
    # anything this script does. The wizard's own comment is explicit that only
    # a user blur may assert that a field has been visited.
    page.locator('[data-testid="agent-description-input"]').click()
    for _ in range(3):
        page.click('[data-testid="wizard-next"]')
        page.wait_for_timeout(150)
    if current_step(page) != 4:
        raise RuntimeError(f"could not reach step 4; stuck on {current_step(page)}")


#: The five states worth measuring: the four steps, with step 3 measured in
#: both tuning modes. `template` is the mode the user LANDS in and is the one
#: W7 is about; `custom` is the one that renders the seven sliders and, at
#: 511px, the collision the plan measures at 31px.
STATES = [
    ("step1", 1, None),
    ("step2", 2, None),
    ("step3-default", 3, False),
    ("step3-custom", 3, True),
    ("step4", 4, None),
]


def drive(page: Page) -> dict:
    """Every state at every viewport, in one pass. Returns {(vp, state): blob}."""
    collected: dict[tuple[tuple[int, int], str], dict] = {}
    for vp in DRIVE_VIEWPORTS:
        page.set_viewport_size({"width": vp[0], "height": vp[1]})
        page.wait_for_timeout(300)
        print(f"  [info] measuring at {vp[0]}x{vp[1]}")
        for state, step, custom in STATES:
            go_to_step(page, step)
            if custom is not None:
                set_tuning_mode(page, custom)
            page.wait_for_timeout(120)
            collected[(vp, state)] = page.evaluate(GEOMETRY_JS)
    return collected


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------


def evaluate_geometry(results: Results, collected: dict) -> None:
    # -- W1 ----------------------------------------------------------------
    # The outcome, not the token. A future `width="xl"` that is somehow
    # overridden by a parent still fails this; a hard-coded 960px that works
    # still passes it.
    print("\n== W1  wizard content width ==")
    for vp, floor in W1_FLOOR.items():
        blob = collected.get((vp, "step1"))
        if blob is None or blob.get("missing"):
            results.unmeasured(
                f"W1 content width at {vp[0]}x{vp[1]}", "the wizard was not on screen"
            )
            continue
        width = blob["contentWidth"]
        results.check(
            f"W1 content width >= {floor}px at {vp[0]}x{vp[1]}",
            width >= floor,
            f"{width}px of {blob['vw']} (was {W1_BASELINE[vp]})",
        )

    # -- W2 ----------------------------------------------------------------
    # Measured in pixels at four widths, per grid. A className assertion is
    # blind to this defect by construction: the class is correct and the box is
    # wrong, so `lg:grid-cols-3` is present on a grid laying 159px cards.
    print("\n== W2  resolved grid tracks, in pixels ==")
    for vp in DRIVE_VIEWPORTS:
        for kind, floor in W2_FLOOR.items():
            state = "step2" if kind == "persona" else "step3-custom"
            blob = collected.get((vp, state))
            grids = [g for g in (blob or {}).get("grids", []) if g["kind"] == kind]
            tracks = [t for g in grids for t in g["tracks"]]
            if not tracks:
                results.unmeasured(
                    f"W2 {kind} tracks >= {floor}px at {vp[0]}x{vp[1]}",
                    f"no {kind} grid rendered in {state}",
                )
                continue
            worst = min(tracks)
            results.check(
                f"W2 {kind} tracks >= {floor}px at {vp[0]}x{vp[1]}",
                worst >= floor,
                f"narrowest {worst}px across {len(grids)} grid(s), "
                f"columns={[len(g['tracks']) for g in grids]}",
            )

    # -- W3 ----------------------------------------------------------------
    # The panel-collapse shape, which is the one failure in this repo that
    # threw nothing: `calc(100dvh - top)` went negative when the chrome above it
    # grew and a chat pane rendered at 24px with no product on it. Asked as
    # "is the body taller than zero" and "is the action on screen", never as
    # "did anything throw".
    print("\n== W3  every step has a body and a reachable action ==")
    for vp in W3_VIEWPORTS:
        heights = []
        missing_body = []
        for state, _, _ in STATES:
            blob = collected.get((vp, state), {})
            if not blob.get("bodyFound"):
                missing_body.append(state)
            else:
                heights.append((blob["bodyClientHeight"], state))
        if missing_body:
            results.check(
                f"W3 scrolling body has height at {vp[0]}x{vp[1]}",
                False,
                f"no scroll container found for: {missing_body}",
            )
        elif not heights:
            results.unmeasured(
                f"W3 scrolling body has height at {vp[0]}x{vp[1]}", "no states measured"
            )
        else:
            worst, where = min(heights)
            results.check(
                f"W3 scrolling body has height at {vp[0]}x{vp[1]}",
                worst > 0,
                f"shortest {worst}px on {where} (the collapse shape was 24px)",
            )

        offscreen = [
            (state, collected[(vp, state)]["footerBottom"], collected[(vp, state)]["vh"])
            for state, _, _ in STATES
            if collected.get((vp, state), {}).get("footerBottom") is not None
            and collected[(vp, state)]["footerBottom"] > collected[(vp, state)]["vh"] + 0.5
        ]
        absent = [
            state
            for state, _, _ in STATES
            if collected.get((vp, state), {}).get("footerTestid") is None
        ]
        if absent:
            results.check(
                f"W3 footer action on screen at {vp[0]}x{vp[1]}",
                False,
                f"no Next/Create button found on: {absent}",
            )
        else:
            bottoms = [
                (collected[(vp, s)]["footerBottom"], s) for s, _, _ in STATES
            ]
            low, where = max(bottoms)
            results.check(
                f"W3 footer action on screen at {vp[0]}x{vp[1]}",
                not offscreen,
                f"lowest bottom {low}px of {vp[1]} on {where}"
                + (f", off screen on {[o[0] for o in offscreen]}" if offscreen else ""),
            )

    # -- W4 ----------------------------------------------------------------
    # BOTH levels, and the second one is where the whole defect lives.
    #
    # The scroll container declares `overflow-y-auto`, and CSS computes the
    # other axis of a non-visible overflow to `auto` as well -- so it scrolls
    # sideways and the DOCUMENT does not. `ui_check.py`'s A7 asks the document
    # only and is structurally incapable of seeing this.
    #
    # The panel-level half is asked TWICE, in two ways that fail
    # independently, and this file has already been wrong once for not doing
    # so. A `scrollWidth > clientWidth` question is only meaningful of the
    # element that actually carries the overflow property, and after WS1 that
    # is the inner body rather than the panel -- so asking it of the panel
    # returned 318 vs 318 while four controls stood 48-58px past the panel's
    # own edge, clipped invisibly by the drawer root's `overflow-hidden`. A
    # green row about the wrong box is worse than no row. So: does the
    # scroller scroll, AND does anything reach past the panel edge at all --
    # the second being a geometric fact that stays true whichever ancestor
    # happens to own `overflow` this month.
    print("\n== W4  zero horizontal overflow at 320px, document AND panel ==")
    vp = NARROW
    for state, _, _ in STATES:
        blob = collected.get((vp, state))
        if blob is None or blob.get("missing"):
            results.unmeasured(f"W4 {state}", "not measured at 320px")
            continue
        results.check(
            f"W4 document has no overflow on {state}",
            blob["docScrollW"] <= blob["docClientW"],
            f"scrollW {blob['docScrollW']} vs clientW {blob['docClientW']}",
        )
        if blob["bodyFound"]:
            scrolled = blob["bodyScrollW"] - blob["bodyClientW"]
            where = "body"
        else:
            scrolled = blob["panelScrollW"] - blob["panelClientW"]
            where = "panel"
        worst = blob["overshoot"] or []
        stick = worst[0]["over"] if worst else 0
        results.check(
            f"W4 panel has no overflow on {state}",
            scrolled <= 0 and stick <= 0.5,
            f"{where} scrolls {scrolled}px, widest overhang {stick}px"
            + (
                "; "
                + ", ".join(f"{w['id']}+{w['over']}px" for w in worst[:3])
                if worst
                else ""
            ),
        )

    # -- W12 ---------------------------------------------------------------
    # Aggregated across the five states so one offending control is one row
    # rather than five, and reported by testid so the row names the fix.
    print("\n== W12  every control >= 44px ==")
    for vp in W12_VIEWPORTS:
        short: dict[str, str] = {}
        for state, _, _ in STATES:
            for control in collected.get((vp, state), {}).get("controls", []):
                # `setdefault`, so a row names the FIRST step a control is too
                # small on. Overwriting named the LAST one and read as "only on
                # step 4" about a rail button that is 36px wide on steps 2, 3
                # and 4 alike -- a true number attached to a false scope.
                if control["h"] < TAP_TARGET:
                    short.setdefault(
                        control["id"], f"{control['id']} h={control['h']} from {state}"
                    )
                elif control["iconOnly"] and control["w"] < TAP_TARGET:
                    short.setdefault(
                        control["id"],
                        f"{control['id']} icon-only w={control['w']} from {state}",
                    )
        counted = sum(
            len(collected.get((vp, s), {}).get("controls", [])) for s, _, _ in STATES
        )
        if counted == 0:
            results.unmeasured(
                f"W12 controls >= 44px at {vp[0]}x{vp[1]}", "no controls found to measure"
            )
            continue
        results.check(
            f"W12 controls >= 44px at {vp[0]}x{vp[1]}",
            not short,
            f"{counted} control readings, {len(short)} under: {list(short.values())[:4]}",
        )


def evaluate_explanations(results: Results, report: dict) -> None:
    """W7, one row per parameter, failing BY NAME.

    Reported at 1440x900 only and deliberately not at four viewports: whether a
    sentence EXISTS is not a fact about the viewport, and printing the same ten
    failures four times would bury the nine other cases. Whether it is visible
    is a fact about the viewport, and that is what `clientHeight > 0` is doing
    inside the measurement.
    """
    print("\n== W7  every parameter explained in the DEFAULT tuning mode ==")
    if report.get("missing"):
        for key in TUNABLE_KEYS:
            results.unmeasured(f"W7 {key}", "the wizard was not on screen")
        return
    for key in TUNABLE_KEYS:
        entry = report.get(key) or {"found": False}
        if not entry.get("found"):
            results.check(
                f"W7 {key} has a visible explanation",
                False,
                "no element on the step names this parameter at all",
            )
            continue
        help_text = entry.get("help")
        if help_text is None:
            results.check(
                f"W7 {key} has a visible explanation",
                False,
                f"label {entry['label']!r} in {entry['cell']}: "
                f"nothing >= {HELP_FLOOR} chars beside it "
                f"(the whole cell is {entry['cellChars']} chars)",
            )
        else:
            results.check(
                f"W7 {key} has a visible explanation",
                help_text["length"] >= HELP_FLOOR and help_text["height"] > 0,
                f"{help_text['length']} chars, {help_text['height']}px: "
                f"{help_text['sample']!r}",
            )


# --------------------------------------------------------------------------
# W10 -- create, then read the row back
# --------------------------------------------------------------------------


def canon(value) -> str:
    """One spelling for a value that crossed a screen and a database.

    The review grid renders booleans as "on"/"off" and numbers through
    JavaScript's own formatting; the API returns `true` and `0.5`. Comparing
    the two as strings would fail on every row and prove nothing, and comparing
    them loosely would pass on a stored `0` against a displayed `off`. This
    normalises both sides to the same small vocabulary and nothing else.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text if text else "0"
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("true", "on", "yes"):
        return "on"
    if lowered in ("false", "off", "no"):
        return "off"
    try:
        return canon(float(text)) if "." in text else canon(int(text))
    except ValueError:
        return lowered


def run_w10(results: Results, page: Page, name: str) -> str | None:
    """A working UI proves an action HAPPENED, never that anything was RECORDED.

    This repo has the scar: a dependency wrote an identity link, flushed, and
    had it rolled back on every request because `get_db` never commits -- the
    sign-in completed, the JWT was correct, the page rendered the user's own
    three agents, and thirty-three cases were green while the row the module
    existed to write never persisted. So the assertion is not "the agent
    appeared on the dashboard". It is: read the ten columns back off the row
    and compare them to the ten values the user was looking at when they
    pressed the button.

    Returns the created agent's id so the caller can delete it, or None.
    """
    print("\n== W10  the ten values on Review are the ten values stored ==")

    page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    page.wait_for_timeout(200)

    # A persona chosen by hand rather than the auto-selected first template, so
    # the row under test carries a template_id somebody actually picked and the
    # baseline tuning is a persona's rather than the server's defaults.
    go_to_step(page, 2)
    cards = page.locator('[data-testid="template-card"]')
    if cards.count() == 0:
        results.unmeasured("W10 stored values match the review", "no personas loaded")
        return None
    cards.nth(min(1, cards.count() - 1)).click()
    page.wait_for_timeout(200)

    # Customize OFF is the mode under test: the request then carries no
    # tunables at all and the SERVER copies the template's values, so this case
    # is checking that the review screen predicts what the server will do --
    # which is the half a create-then-read-back can actually falsify.
    go_to_step(page, 3)
    set_tuning_mode(page, False)

    go_to_step(page, 4)
    shown = page.evaluate(REVIEW_JS, {"aliases": TUNABLE_ALIASES})
    if not shown:
        results.unmeasured(
            "W10 stored values match the review",
            "the review parameter grid could not be read; update REVIEW_JS",
        )
        return None
    missing = [key for key in TUNABLE_KEYS if key not in shown]
    if missing:
        results.check(
            "W10 the review shows all ten parameters",
            False,
            f"missing from the review: {missing}",
        )
    else:
        results.check(
            "W10 the review shows all ten parameters", True, "all ten read off screen"
        )

    # Printed BEFORE the POST, not after. A process killed between the commit
    # and the sweep leaves a row whose only handle is its name, and a name
    # printed afterwards is a name that was never printed.
    print(f"  [info] about to CREATE agent named {name!r} -- delete by this name if orphaned")
    page.click('[data-testid="agent-create-submit"]')
    page.wait_for_selector('[data-testid="agent-shell"]', timeout=30_000)

    listed = page.request.get(f"{API}/api/agents")
    if not listed.ok:
        results.check("W10 stored values match the review", False,
                      f"could not list agents: {listed.status}")
        return None
    row = next((a for a in listed.json() if a["name"] == name), None)
    if row is None:
        results.check(
            "W10 stored values match the review",
            False,
            "the wizard reported success and no row with that name exists",
        )
        return None

    agent_id = row["id"]
    print(f"  [info] created agent {agent_id}")
    fetched = page.request.get(f"{API}/api/agents/{agent_id}")
    if not fetched.ok:
        results.check("W10 stored values match the review", False,
                      f"GET /api/agents/{agent_id} -> {fetched.status}")
        return agent_id
    stored = fetched.json()

    mismatches = []
    for key in TUNABLE_KEYS:
        if key not in shown or key not in stored:
            mismatches.append(f"{key}: shown={shown.get(key)!r} stored=<absent>")
            continue
        if canon(shown[key]) != canon(stored[key]):
            mismatches.append(f"{key}: shown={shown[key]!r} stored={stored[key]!r}")
    results.check(
        "W10 stored values match the review",
        not mismatches,
        f"{len(TUNABLE_KEYS) - len(mismatches)}/10 columns agree"
        + (f"; {mismatches}" if mismatches else ""),
    )
    return agent_id


# --------------------------------------------------------------------------
# Ownership: counting, and the sweep
# --------------------------------------------------------------------------


def list_agents(request) -> list[dict]:
    response = request.get(f"{API}/api/agents")
    return response.json() if response.ok else []


def sweep(request, label: str) -> int:
    """Delete every agent this harness could have made. Idempotent.

    Filtered on the prefix and nothing else. `select(User).limit(1)` is the
    defect this rule exists to prevent -- no ORDER BY, so which real person
    gets a harness fixture in their dashboard is whatever Postgres returns
    first -- and the equivalent here would be deleting "the newest agent". The
    only rows touched are rows whose NAME this file wrote.
    """
    mine = [a for a in list_agents(request) if a["name"].startswith(AGENT_PREFIX)]
    if not mine:
        print(f"  [info] {label}: nothing to clean up")
        return 0
    for agent in mine:
        response = request.delete(f"{API}/api/agents/{agent['id']}")
        print(f"  [info] {label}: deleted {agent['name']!r} -> {response.status}")
    return len(mine)


def run_cleanup() -> int:
    """The sweep on its own, because a `finally` does not cover a killed process.

    No browser: an API request context signs in through the same dev-login
    route and carries the same cookie, which is all the sweep needs.
    """
    with sync_playwright() as pw:
        request = pw.request.new_context()
        try:
            login = request.post(
                f"{API}/api/auth/dev-login", data={"email": DEV_EMAIL}
            )
            if not login.ok:
                print(f"  [FAIL] dev-login refused: {login.status} {login.text()[:160]}")
                print("         Local dev needs DEV_AUTH_ENABLED=true and")
                print("         ENVIRONMENT=development in backend/.env.")
                return 2
            removed = sweep(request, "cleanup")
            print(f"  [info] {len(list_agents(request))} agents remain on {DEV_EMAIL}")
            print(f"cleanup removed {removed}")
            return 0
        finally:
            request.dispose()


# --------------------------------------------------------------------------


#: W6. The dialog cannot be pushed off its own screen.
#:
#: Found by eye, not by a case, which is why the case exists. The drawer root is
#: `fixed inset-0 overflow-hidden` and the panel WAS `overflow: visible`, so the
#: panel's 2,012px of scrollable overflow propagated to the root -- and an
#: `overflow: hidden` box is a scrollport that is invisible to the user and
#: fully scrollable to script. Measured before the fix: root `scrollTop` 70,
#: `scrollHeight` 2058 against a 900px client, the centred panel dragged from
#: top 45 to top -25, and its own title clipped off the top of the screen.
#:
#: The trigger is ordinary. `scrollIntoView` does it, and so does moving FOCUS
#: to a control low in a tall panel, because the browser scrolls every ancestor
#: scrollport to reveal the focused element. So this drives BOTH, and it drives
#: them on step 3 in Customize mode, which is the tallest screen in the flow.
#:
#: It asserts the OUTCOME -- is the header still where it was, are the close
#: button and the step rail still on screen -- rather than the CSS property that
#: currently delivers it. `overflow-clip` is the fix today; a case asserting
#: `overflow-clip` would pass on a future refactor that kept the property and
#: lost the behaviour, and would fail on one that reached the same outcome
#: another way.
DISPLACEMENT_JS = r"""
async ({ lowControl, group }) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const panel = document.querySelector('[data-testid="create-agent-panel"]');
  if (!panel) return null;
  const root = panel.parentElement;
  const header = panel.querySelector('h2');
  const rail = document.querySelector('[data-testid="wizard-rail"]');
  const close = panel.querySelector('[data-testid="create-agent-panel-close"]');
  if (!header || !rail || !close) return null;

  const headerBefore = Math.round(header.getBoundingClientRect().top);

  // Every road to the same displacement, together.
  panel.scrollTop = 400;
  root.scrollTop = 400;
  const target = document.querySelector(group);
  if (target) target.scrollIntoView({ block: 'center' });
  const low = document.querySelector(lowControl);
  if (low) low.focus();
  await sleep(300);

  const onScreen = (el) => {
    const r = el.getBoundingClientRect();
    return r.height > 0 && r.top >= -0.5 && r.bottom <= window.innerHeight + 0.5;
  };

  return {
    panelScrollTop: panel.scrollTop,
    rootScrollTop: root.scrollTop,
    headerBefore,
    headerAfter: Math.round(header.getBoundingClientRect().top),
    closeOnScreen: onScreen(close),
    railOnScreen: onScreen(rail),
    focused: low ? document.activeElement === low : null,
  };
}
"""


def evaluate_displacement(results: "Results", seen) -> None:
    """W6, the three ways the dialog could be pushed off screen."""
    print("\n== W6  the dialog cannot be pushed off its own screen ==")
    if not seen:
        results.unmeasured(
            "W6 the dialog stays where it was put",
            "the panel, its heading, its close button or the rail was not found",
        )
        return

    moved = seen["headerAfter"] - seen["headerBefore"]
    results.check(
        "W6 the dialog stays where it was put",
        moved == 0,
        f"heading moved {moved}px (was y={seen['headerBefore']}, "
        f"now y={seen['headerAfter']}); the bug moved it -70px",
    )
    results.check(
        "W6 neither the root nor the panel is a scrollport",
        seen["panelScrollTop"] == 0 and seen["rootScrollTop"] == 0,
        f"panel scrollTop {seen['panelScrollTop']}, root scrollTop "
        f"{seen['rootScrollTop']} after being told to scroll to 400",
    )
    results.check(
        "W6 close and the step rail stay reachable at the bottom of the tallest step",
        seen["closeOnScreen"] and seen["railOnScreen"],
        f"close on screen: {seen['closeOnScreen']}, rail on screen: "
        f"{seen['railOnScreen']}",
    )


#: W14. The selected persona LOOKS selected.
#:
#: Only a real browser can answer this. jsdom has no stylesheet, so the unit
#: suite cannot tell `bg-accent-soft` from nothing at all -- and neither can
#: reading the class list, because the class was present and correct the whole
#: time it was doing nothing.
#:
#: `${CARD} bg-accent-soft` ties on specificity with `CARD`'s own `bg-surface`,
#: and Tailwind's emitted order decides: measured in `dist`, `.bg-accent-soft`
#: at byte 18350 against `.bg-surface` at 19770, so the shared one wins. The
#: selected card had carried that class since it was written, under a comment
#: describing "an accent border and an accent-soft fill", and the fill had
#: never rendered once.
#:
#: Asserts that the two backgrounds DIFFER, not what either colour is. The
#: palette is themed and a value assertion would be a second place to keep the
#: tokens in step; "selected looks different from unselected" is the thing a
#: user needs and is true in both themes.
SELECTION_JS = r"""
() => {
  const cards = [...document.querySelectorAll('[data-testid="template-card"]')];
  if (cards.length < 2) return null;
  const selected = cards.find((c) => c.dataset.selected === 'true');
  const other = cards.find((c) => c.dataset.selected !== 'true');
  if (!selected || !other) return null;
  const bg = (el) => getComputedStyle(el).backgroundColor;
  const border = (el) => getComputedStyle(el).borderTopColor;
  return {
    selectedBg: bg(selected),
    otherBg: bg(other),
    selectedBorder: border(selected),
    otherBorder: border(other),
  };
}
"""


def evaluate_selection(results: "Results", seen) -> None:
    """W14, whether choosing a persona is visible."""
    print("\n== W14  the selected persona looks selected ==")
    if not seen:
        results.unmeasured(
            "W14 the selected persona is filled differently",
            "fewer than two persona cards, or none selected",
        )
        return

    results.check(
        "W14 the selected persona is filled differently",
        seen["selectedBg"] != seen["otherBg"],
        f"selected {seen['selectedBg']} vs unselected {seen['otherBg']}"
        + ("  <- identical: the fill class is being overridden" if seen["selectedBg"] == seen["otherBg"] else ""),
    )
    results.check(
        "W14 the selected persona is outlined differently",
        seen["selectedBorder"] != seen["otherBorder"],
        f"selected {seen['selectedBorder']} vs unselected {seen['otherBorder']}",
    )


#: W15. The reset notice ANNOUNCES.
#:
#: No harness can hear a screen reader, so this asserts the one structural fact
#: that decides whether anything is spoken: an `aria-live` region announces a
#: CHANGE to its contents, and a region that arrives on the page already
#: holding its text has changed nothing. A notice rendered as
#: `{resetNotice && <p role="status">...}` is mounted and populated in the same
#: commit and is therefore silent -- which means the sentence explaining that
#: customised tuning was discarded has almost certainly never been spoken once,
#: and that notice exists precisely to stop a silent reset.
#:
#: Two reads, and the second is only meaningful because of what the first
#: leaves behind. The first stamps every live region in the wizard with a
#: PROPERTY on the DOM node -- never a `data-` attribute, which React owns and
#: overwrites on its next commit. A property dies with the NODE, so "the same
#: element later carries the text" and "a new element appeared holding the
#: text" come back as different answers instead of both reading as a live
#: region with a notice in it.
#:
#: The two failures are reported apart on purpose. "No live region present
#: before the notice" sends its reader to add one; "a live region appeared with
#: the text already in it" sends them to lift the region out of the conditional
#: it is trapped in. Collapsing them into "the notice does not announce" would
#: name the symptom and neither cause.
LIVE_REGION_STAMP = "wizard-check-w15"

#: The sentence the wizard writes when a persona change discards tuning. It is
#: the SECOND road to the element, never the first -- see the JS below.
RESET_NOTICE_PATTERN = r"tuning reset to"

LIVE_REGION_STAMP_JS = r"""
({ stamp }) => {
  const wizard = document.querySelector('[data-testid="create-agent-wizard"]');
  if (!wizard) return null;

  // Scanned wide, judged narrow. An assertive region would announce too, and
  // would be the wrong choice for this notice for the reason `ParamSlider`
  // already writes down about the overlap warning -- a value-driven message
  // re-renders on every step of a drag, and an assertive region interrupts for
  // the length of the gesture. Finding one anyway is a different bug from
  // finding none, and a scan that looked only for `polite` would report the
  // two identically.
  const found = [...wizard.querySelectorAll('[role="status"], [role="alert"], [aria-live]')];

  return {
    regions: found.map((el) => {
      // An expando property, not `dataset`. React writes the attributes it
      // renders and would wipe a `data-` marker on its next commit; a property
      // on the node survives every re-render and dies with the node. That is
      // the whole discrimination this case rests on.
      el.__wizardCheckLiveRegion = stamp;
      const r = el.getBoundingClientRect();
      return {
        testid: el.dataset.testid || null,
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role'),
        ariaLive: el.getAttribute('aria-live'),
        chars: (el.innerText || '').trim().length,
        // Reported rather than asserted. An empty live region must not draw a
        // bordered box, and the honest floor for "draws nothing" is not a
        // number this file can pick -- an `sr-only` region is 1px tall and
        // correct. A height printed beside a zero character count is enough
        // for a reader to see a box that should not be there.
        height: +r.height.toFixed(1),
      };
    }),
  };
}
"""

LIVE_REGION_READ_JS = r"""
({ stamp, pattern }) => {
  const wizard = document.querySelector('[data-testid="create-agent-wizard"]');
  if (!wizard) return null;
  const re = new RegExp(pattern, 'i');

  // The testid is the first road and the copy is the second, because the two
  // fail in opposite directions. A rewrite that keeps the testid and rewrites
  // the sentence breaks the regex; a rewrite that moves the notice into a
  // region carrying a different testid breaks the selector. With both, "no
  // notice appeared" is a finding about the product rather than about which
  // string this file happened to hardcode.
  let notice = wizard.querySelector('[data-testid="tuning-reset-notice"]');
  if (!notice || !re.test((notice.innerText || '').trim())) {
    const hits = [...wizard.querySelectorAll('*')].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.height > 0 && re.test((el.innerText || '').trim());
    });
    // Smallest by descendant count. Every ancestor of the notice contains the
    // same sentence and matches the same regex, and an ancestor is the wrong
    // element to ask "is this the live region" of -- a region wrapped around
    // the whole step would answer yes while announcing the entire form.
    hits.sort((a, b) =>
      a.getElementsByTagName('*').length - b.getElementsByTagName('*').length);
    if (hits.length) notice = hits[0];
  }
  if (!notice || !re.test((notice.innerText || '').trim())) {
    return { noticeFound: false };
  }

  // Climb to the nearest live region, the notice itself included, and stop at
  // the wizard. A region ABOVE the wizard belongs to the drawer or the page and
  // would announce every step change as well as this sentence -- a second
  // announcement for something the focus move to each step's heading already
  // says.
  let region = null;
  for (let node = notice; node; node = node.parentElement) {
    if (node.matches('[role="status"], [role="alert"], [aria-live]')) { region = node; break; }
    if (node === wizard) break;
  }

  return {
    noticeFound: true,
    noticeText: (notice.innerText || '').trim().slice(0, 72).replace(/\s+/g, ' '),
    noticeTestid: notice.dataset.testid || null,
    region: region
      ? {
          stamped: region.__wizardCheckLiveRegion === stamp,
          testid: region.dataset.testid || null,
          tag: region.tagName.toLowerCase(),
          role: region.getAttribute('role'),
          ariaLive: region.getAttribute('aria-live'),
          isWizard: region === wizard,
        }
      : null,
  };
}
"""


def produce_reset_notice(page: Page) -> dict:
    """Drive the one sequence that writes the notice, stamping first.

    Every step of it is required and none of it is a shortcut. The notice is
    gated on `tuningTouched`, which only a real edit sets -- deliberately, so
    that browsing personas with the toggle left on does not announce a loss to
    somebody who has customised nothing. And it is gated on the PREVIOUS
    render's flags, so the edit has to happen before the persona changes rather
    than after. Hence: land on the tuning step in Customize, move a control, go
    back a step, choose a different persona, come forward again.

    Both preconditions are verified rather than assumed. If the slider did not
    move or the persona did not change, no notice was ever owed and its absence
    says nothing -- a fixture failure, reported as NOT MEASURED. If both held
    and no notice appeared, the reset happened silently, which is the defect
    itself and is reported as a FAIL. Trigger on the absence of the outcome,
    never on the presence of an error.
    """
    seen: dict = {"blocked": None, "before": None, "after": None}

    page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    go_to_step(page, 3)
    set_tuning_mode(page, True)
    page.wait_for_timeout(200)

    seen["before"] = page.evaluate(LIVE_REGION_STAMP_JS, {"stamp": LIVE_REGION_STAMP})
    if seen["before"] is None:
        seen["blocked"] = "the wizard was not on screen"
        return seen

    # `retrieve_k`, and not the overlap, for two reasons that both outlive this
    # run: the overlap is the one control whose rule BLOCKS Next, so moving it
    # can strand the drive on step 3; and `retrieve_k` sits in the group that
    # takes effect on the next answer, which is a group that stays expanded.
    param = page.locator('[data-testid="param-retrieve-k"]')
    if param.count() == 0:
        seen["blocked"] = "no retrieve_k control in Customize mode"
        return seen
    was = param.get_attribute("data-value")

    # `focus()` and an arrow key, never a click. Clicking a range input sets it
    # to the position clicked, so a click is an edit whose size depends on where
    # the track happens to be -- and one that lands on the value already
    # selected changes nothing at all while looking like an interaction.
    page.focus('[data-testid="param-retrieve-k-range"]')
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(150)
    if param.get_attribute("data-value") == was:
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(150)
    now = param.get_attribute("data-value")
    if now == was:
        seen["blocked"] = f"the retrieve_k slider did not move off {was!r}"
        return seen
    seen["edit"] = f"retrieve_k {was} -> {now}"

    go_to_step(page, 2)
    cards = page.locator('[data-testid="template-card"]')
    count = cards.count()
    if count < 2:
        seen["blocked"] = f"{count} persona card(s); a change needs two"
        return seen
    chosen = -1
    for index in range(count):
        if cards.nth(index).get_attribute("data-selected") == "true":
            chosen = index
            break
    target = 0 if chosen != 0 else 1
    cards.nth(target).click()
    page.wait_for_timeout(300)
    if cards.nth(target).get_attribute("data-selected") != "true":
        seen["blocked"] = f"clicking persona {target} did not select it"
        return seen
    seen["persona"] = f"persona {chosen} -> {target}"

    go_to_step(page, 3)
    page.wait_for_timeout(250)
    seen["after"] = page.evaluate(
        LIVE_REGION_READ_JS,
        {"stamp": LIVE_REGION_STAMP, "pattern": RESET_NOTICE_PATTERN},
    )
    return seen


def evaluate_live_region(results: "Results", seen: dict) -> None:
    """W15, the two halves of "it is able to announce"."""
    print("\n== W15  the reset notice announces ==")

    if seen.get("blocked"):
        for half in (
            "W15 a live region is mounted before the notice has text",
            "W15 the notice's text lands in that same live region",
        ):
            results.unmeasured(half, f"no notice was owed: {seen['blocked']}")
        return

    before = seen["before"]["regions"]
    empty = [r for r in before if r["chars"] == 0]
    shape = ", ".join(
        f"{r['testid'] or r['tag']}(role={r['role']}, live={r['ariaLive']}, "
        f"{r['chars']}ch, {r['height']}px)"
        for r in before
    )
    # Polite, and stated as an accepted SET rather than read off `role` alone:
    # `role="status"` carries an implicit `aria-live="polite"`, so the two
    # spellings are one promise and only one of them is visible in the markup.
    polite = [
        r
        for r in empty
        if r["role"] == "status" or (r["ariaLive"] or "").lower() == "polite"
    ]
    results.check(
        "W15 a live region is mounted before the notice has text",
        bool(polite),
        (
            f"{len(before)} live region(s) in the wizard, {len(empty)} of them empty"
            + (f": {shape}" if before else "; nothing carries role=status or aria-live")
        ),
    )

    after = seen.get("after")
    if not after or not after.get("noticeFound"):
        results.check(
            "W15 the notice's text lands in that same live region",
            False,
            f"the tuning WAS reset ({seen.get('edit')}, {seen.get('persona')}) "
            "and no notice appeared anywhere: the reset was silent",
        )
        return

    region = after.get("region")
    if region is None:
        results.check(
            "W15 the notice's text lands in that same live region",
            False,
            # `ascii()` on anything read off the page: this sentence is written
            # by the product and a curly apostrophe in it would be mangled by
            # the console codepage into a row nobody can read.
            f"the notice rendered in {after['noticeTestid'] or 'an untagged element'} "
            f"({ascii(after['noticeText'])}) with no live region anywhere above it",
        )
        return

    results.check(
        "W15 the notice's text lands in that same live region",
        bool(region["stamped"]) and not region["isWizard"],
        (
            f"region {region['testid'] or region['tag']} "
            f"(role={region['role']}, live={region['ariaLive']}) "
            + (
                "was on the page before the notice was"
                if region["stamped"]
                else "was NOT on the page before the notice was, so it arrived "
                "already containing its text and announces nothing"
            )
            + (
                "; it is the whole wizard, which would announce every step change"
                if region["isWizard"]
                else ""
            )
        ),
    )


#: W16. Below `sm` the dialog is a full-bleed SHEET, not a centred card.
#:
#: `17-create-agent-ux/PLAN.md` section 2 defends centring against the inline-page
#: measurement in `05-ui-ux-overhaul.md` with the sentence "below sm the panel
#: stays full-bleed, so the phone case 05 fixed is bit-identical". The panel is
#: `h-fit max-h-[...] rounded-lg border`, so a short step renders as a floating
#: card on a phone and the claim is not true. A defence resting on a property
#: nothing asserts is a defence that expires quietly.
#:
#: Measured on step 1, which is the SHORTEST step, and that choice is the case.
#: On the tuning step the content is taller than a phone, the panel hits its cap
#: and covers nearly the whole viewport whatever its sizing rules say -- so a
#: full-bleed assertion taken there would pass a floating card on the strength
#: of how much text happened to be inside it.
#:
#: The desktop half asserts the OPPOSITE, and without it the case has a trivial
#: satisfying answer: make every viewport full-bleed and lose the centred dialog
#: this whole change set exists to build.
SHEET_JS = r"""
() => {
  const panel = document.querySelector('[data-testid="create-agent-panel"]');
  if (!panel) return null;
  const r = panel.getBoundingClientRect();
  const s = getComputedStyle(panel);
  const px = (v) => +parseFloat(v || '0').toFixed(1);
  return {
    top: +r.top.toFixed(1),
    left: +r.left.toFixed(1),
    right: +r.right.toFixed(1),
    bottom: +r.bottom.toFixed(1),
    width: +r.width.toFixed(1),
    height: +r.height.toFixed(1),
    vw: innerWidth,
    vh: innerHeight,
    radii: [
      px(s.borderTopLeftRadius), px(s.borderTopRightRadius),
      px(s.borderBottomRightRadius), px(s.borderBottomLeftRadius),
    ],
    // Reported, not asserted. A border-box panel is exactly `innerWidth` wide
    // with or without a border, so the rect cannot see one -- and the sheet is
    // specified with no outer border, so the number is printed where a reader
    // will notice a stray one.
    borders: [
      px(s.borderTopWidth), px(s.borderRightWidth),
      px(s.borderBottomWidth), px(s.borderLeftWidth),
    ],
  };
}
"""

#: One pixel of slack, the tolerance every other geometric assertion in this
#: file already carries. Sub-pixel layout rounding is not a defect, and a case
#: that fails on 0.4px teaches its reader to re-run rather than to read.
SHEET_SLACK = 1.0


def measure_sheet(page: Page) -> dict:
    """The panel's box at three viewports, always on step 1. See SHEET_JS."""
    seen: dict = {}
    for vp in (PHONE, NARROW, DESKTOP):
        page.set_viewport_size({"width": vp[0], "height": vp[1]})
        go_to_step(page, 1)
        # The panel's transition is on `transform` and `opacity` for the centred
        # placement, and a resize re-runs the layout underneath it. Measured
        # during that, the rect is a real box in the wrong place.
        page.wait_for_timeout(350)
        seen[vp] = page.evaluate(SHEET_JS)
    return seen


def evaluate_sheet(results: "Results", seen: dict) -> None:
    """W16, the sheet below `sm` and the card above it."""
    print("\n== W16  below sm the dialog is a full-bleed sheet ==")

    for vp in (PHONE, NARROW):
        blob = seen.get(vp)
        label = f"W16 full-bleed sheet at {vp[0]}x{vp[1]}"
        if not blob:
            results.unmeasured(label, "the panel was not on screen")
            continue
        gaps = {
            "top": blob["top"],
            "left": blob["left"],
            "width": blob["width"] - blob["vw"],
            "height": blob["height"] - blob["vh"],
        }
        covers = all(abs(v) <= SHEET_SLACK for v in gaps.values())
        radius = max(blob["radii"])
        results.check(
            label,
            covers and radius <= 0.5,
            f"rect {blob['width']}x{blob['height']} at ({blob['left']},{blob['top']}) "
            f"of {blob['vw']}x{blob['vh']}, radius {blob['radii']}, "
            f"border {blob['borders']}"
            + (
                ""
                if covers
                else "; off by "
                + ", ".join(
                    f"{k} {v:+.1f}" for k, v in gaps.items() if abs(v) > SHEET_SLACK
                )
            ),
        )

    blob = seen.get(DESKTOP)
    label = f"W16 centred card at {DESKTOP[0]}x{DESKTOP[1]}"
    if not blob:
        results.unmeasured(label, "the panel was not on screen")
        return
    inset = {
        "top": blob["top"],
        "left": blob["left"],
        "right": blob["vw"] - blob["right"],
        "bottom": blob["vh"] - blob["bottom"],
    }
    boxed = all(v > SHEET_SLACK for v in inset.values())
    radius = min(blob["radii"])
    results.check(
        label,
        boxed and radius > 0.5,
        f"gaps { {k: round(v, 1) for k, v in inset.items()} }, radius {blob['radii']}"
        + ("" if boxed else "; the card is touching an edge, so it is not centred"),
    )


#: W17 WAS HERE, and it is gone because the behaviour it asserted was reversed.
#:
#: It checked that the "recorded, but changes nothing today" group arrived
#: collapsed. That shipped, and W7 -- which asserts that every one of the ten
#: parameters carries a visible explanation IN THE MODE THE USER LANDS IN --
#: went red on `score_threshold` and `max_rewrites` the moment it did. The two
#: criteria are not both satisfiable: collapsing a group is precisely the act of
#: taking its explanations off the arrival screen, and W7 is the case that
#: encodes what this whole step was rebuilt to fix.
#:
#: W7 won, so the collapse was reverted and W17 has no subject left. It is
#: DELETED rather than skipped or left red -- a case that cannot pass is noise,
#: and a skipped one is a claim that the behaviour is merely unmeasured when in
#: fact it was decided against.
#:
#: Two things it found before it died are worth keeping, because both are about
#: this harness rather than about the product:
#:
#: - Its first draft asked "is there a <summary> here, and does clicking it
#:   reveal something". That went GREEN against code with no group disclosure at
#:   all, because it found the first per-parameter "Why this matters" Reveal and
#:   opened that instead. A structural assertion has to resolve the SPECIFIC
#:   element it means, never the first one of its kind in the subtree.
#: - Its visibility predicate read `getBoundingClientRect().height > 0`, on the
#:   stated premise that a closed <details>'s content measures 0x0. Measured in
#:   the Chromium bundled here (148.0.7778.96), it does NOT -- closed content
#:   reported 60x1280 with `checkVisibility()` false. W7's own gate is
#:   `innerText`, which is rendering-aware and got the same page right in the
#:   same drive. Use `checkVisibility()` or text, never a rect, to ask whether
#:   something is on screen.
def run(headed: bool, live: bool) -> int:
    results = Results()
    created_id: str | None = None
    agent_name = f"{AGENT_PREFIX}{uuid.uuid4().hex[:8]}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": DESKTOP[0], "height": DESKTOP[1]}
        )
        page = context.new_page()

        print("\n== opening the create wizard ==")
        if not sign_in(page):
            browser.close()
            return 2

        errors: list[str] = []
        # Attached AFTER sign-in, and not filtered on the text "401". The
        # signed-out screen probes /api/auth/me and is answered 401 by design --
        # that is the app correctly discovering there is no session, not an
        # error on any screen under test. Filtering by text would also hide a
        # genuine 401 from a later request; attaching late scopes the assertion
        # to the thing being driven instead of weakening it.
        # Gateway failures on the Better Auth proxy, kept apart from real
        # errors. `vite.config.ts` forwards `/api/auth/*` to the Node service
        # on :3000 so the session cookie is first-party in development the way
        # it is in production; with that process not running, Vite answers 502
        # and the SPA's token probe logs it on every screen. This harness signs
        # in through the dev-login shim and never touches that road, so the row
        # says nothing about the wizard.
        #
        # Reported as NOT MEASURED rather than filtered, and rather than
        # failed. `agentic_check.py` prints `[rate]` instead of `[FAIL]` for an
        # upstream refusal for exactly this reason: a suite that goes red
        # because a service nobody started said no teaches its reader to ignore
        # red. Deleting the line instead would be worse -- it would hide a real
        # auth failure behind an environment note.
        gateway: list[str] = []

        def record(message) -> None:
            text = message.text
            if message.type != "error":
                return
            if "favicon" in text.lower() or "[vite]" in text.lower():
                return
            # The URL, not only the sentence. Chromium's message for a failed
            # subresource is "Failed to load resource: the server responded
            # with a status of 502" and names nothing -- a row that sends its
            # reader looking through four files for a request it cannot
            # identify is a row that gets ignored, which is the same cost as
            # not having it.
            url = (message.location or {}).get("url") or ""
            line = f"{text} <- {url}" if url else text
            gateway_status = any(code in text for code in ("502", "503", "504"))
            if "/api/auth/" in url and gateway_status:
                gateway.append(line)
            else:
                errors.append(line)

        page.on("console", record)

        before = len(list_agents(page.request))
        print(f"  [info] {before} agents on {DEV_EMAIL} before this run")

        try:
            open_wizard(page)
            # The name is a `wizard-check ` name even on a run that never
            # submits, so an accidental create is findable by the same sweep.
            print(f"  [info] wizard name for this run: {agent_name!r}")
            unlock_rail(page, agent_name)

            collected = drive(page)
            evaluate_geometry(results, collected)

            # W14 asks step 2, where a persona has been chosen by the drive.
            go_to_step(page, 2)
            page.wait_for_timeout(200)
            evaluate_selection(results, page.evaluate(SELECTION_JS))

            # W6 needs the TALLEST screen in the flow, which is step 3 with the
            # controls shown, so it runs before W7 puts the mode back.
            page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
            go_to_step(page, 3)
            set_tuning_mode(page, True)
            page.wait_for_timeout(250)
            evaluate_displacement(
                results,
                page.evaluate(
                    DISPLACEMENT_JS,
                    {
                        "lowControl": '[data-testid="param-max-rewrites-number"]',
                        "group": '[data-testid="tuning-group-inert"]',
                    },
                ),
            )

            # W7 is asked in the mode the user LANDS in. Stated as a click
            # rather than assumed, because the drive above left step 3 in
            # Customize and a case that measured the wrong mode would find
            # seven help strings and report a pass on the surface that has
            # none.
            page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
            go_to_step(page, 3)
            set_tuning_mode(page, False)
            page.wait_for_timeout(200)
            evaluate_explanations(
                results,
                page.evaluate(
                    EXPLANATION_JS, {"aliases": TUNABLE_ALIASES, "floor": HELP_FLOOR}
                ),
            )

            # W15 before W16, and both before W10, which submits
            # the form and takes the wizard off the page. W15 is the only one
            # that leaves state behind -- a different persona and a notice on
            # the tuning step -- and W16's first move is back to step 1, which
            # is also the click that clears the notice.
            evaluate_live_region(results, produce_reset_notice(page))
            evaluate_sheet(results, measure_sheet(page))

            if live:
                created_id = run_w10(results, page, agent_name)
            else:
                results.unmeasured(
                    "W10 stored values match the review",
                    "--live not passed, so nothing was written to the database",
                )

            print("\n== W13  console ==")
            # Deduplicated, because one broken subresource polled on a timer
            # produces the same line twenty times and buries the second fault.
            unique = list(dict.fromkeys(errors))
            for line in unique:
                print(f"         {line}")
            if gateway:
                results.unmeasured(
                    "W13 the Better Auth proxy answered",
                    f"{len(gateway)} gateway errors on /api/auth "
                    f"({list(dict.fromkeys(gateway))[0][:120]}); "
                    "start the Node service on :3000 to measure this road",
                )
            results.check(
                "W13 zero console errors across the whole drive",
                not errors,
                f"{len(errors)} errors, {len(unique)} distinct"
                + (f": {unique[0][:180]}" if unique else ""),
            )

        finally:
            # Both roads back: the id if the create returned one, and the name
            # sweep for the case where it did not (a create that succeeded on
            # the server and failed to report is exactly the shape this covers).
            if created_id:
                response = page.request.delete(f"{API}/api/agents/{created_id}")
                print(f"  [info] deleted {created_id} -> {response.status}")
            sweep(page.request, "finally")
            after = len(list_agents(page.request))
            print(f"  [info] {after} agents on {DEV_EMAIL} after cleanup (was {before})")
            if after != before:
                print(f"  [FAIL] agent count moved: {before} -> {after}")
                results.failed.append(
                    f"cleanup: agent count moved {before} -> {after}"
                )
            browser.close()

    print("\n" + "=" * 68)
    print(
        f"passed {len(results.passed)}   failed {len(results.failed)}"
        f"   not measured {len(results.unrun)}"
    )
    for line in results.failed:
        print(f"  FAILED       {line}")
    # Printed even on a green run: an unmeasured assertion that scrolls past in
    # silence is indistinguishable from one that passed.
    for line in results.unrun:
        print(f"  NOT MEASURED {line}")
    print("=" * 68)
    return 1 if results.failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Browser assertions for the create-agent wizard.",
        epilog="Both servers must be running, and the frontend must be on port 5173.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Watch it run. Useful when a number alone does not say why.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run W10, which CREATES one agent and deletes it again.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete any 'wizard-check ' agent a killed run left behind, and exit.",
    )
    args = parser.parse_args()
    if args.cleanup:
        raise SystemExit(run_cleanup())
    raise SystemExit(run(args.headed, args.live))
